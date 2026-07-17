"""Fused ``Stop`` hook — one interpreter start dispatches every Stop guard.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as the
SINGLE ``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.Stop``
array (see :func:`hpc_agent.agent_assets.install_agent_assets`). It replaces the
three legacy standalone Stop entries — ``skill_return_stop_guard``,
``decision_rendezvous_stop_guard``, and the ``relay_audit_stop`` package — so a
Stop event costs ONE Python interpreter start (and ONE ``hpc_agent`` import)
instead of three (#288: a Python start is ~300-500ms on Windows; the three-guard
trio paid that ~3× per turn plus 3× the import tax).

Why a multiplexer, not a merged guard
-------------------------------------
Each guard's decision logic stays its own ONE definition — the multiplexer only
DISPATCHES. It reads the Stop payload once, hands the SAME parsed payload to each
guard's ``build_hook_output``, and composes the guards' outputs into one hook
result. No guard predicate is re-implemented here; the guards are imported and
called unchanged.

The installed command names the guard modules explicitly as arguments::

    <python> -m hpc_agent._kernel.hooks.stop_multiplex \
        hpc_agent._kernel.hooks.skill_return_stop_guard \
        hpc_agent._kernel.hooks.decision_rendezvous_stop_guard \
        hpc_agent._kernel.hooks.relay_audit_stop

so the fused entry's command STILL mentions each legacy needle — the capability
probe (:func:`hpc_agent.ops.harness_capabilities._needle_installed`, keyed on
``_RELAY_AUDIT_NEEDLE``) and the re-find matcher
(:func:`hpc_agent.agent_assets._find_hook_entry_index`) both continue to resolve
against the fused entry with no change to their needle constants. A bare
``python -m …stop_multiplex`` (no args) falls back to :data:`_DEFAULT_GUARDS`.

Fail toward running (technical F1)
----------------------------------
* The stdin read is robust: :func:`read_stdin_payload` reads ``sys.stdin.buffer``
  and decodes utf-8 with ``errors="replace"`` — a non-utf8 byte can never crash
  the fused hook (a decode failure degrades to replacement chars, then normal
  JSON parsing; a non-JSON payload yields ``None`` and every guard no-ops).
* Per-guard isolation: each guard runs in its own ``try`` — guard A raising still
  runs B and C, and A simply contributes no output. One broken guard never wedges
  the others (or the harness).
* First-block-wins: the FIRST guard (in dispatch order) that returns a
  ``decision: block`` supplies the block reason, exactly as Claude Code would pick
  the first blocking Stop hook. A guard blocking never suppresses another guard's
  ACCOUNTING side-effects (the relay-audit discharge/provenance journaling runs
  inside ``build_hook_output`` and has already happened by the time we compose the
  output — the relay-audit seat is preserved), and any guard's ``systemMessage``
  is carried through even when a different guard supplies the block.

The syntactic prefilter (necessary-condition, stdlib-only)
----------------------------------------------------------
:func:`prefilter_should_run` is a cheap, stdlib-only *necessary* condition for ANY
guard to do work — it NEVER imports :mod:`hpc_agent` and NEVER re-implements a
guard predicate. It skips (returns ``False`` → the whole hook exits ``0`` having
imported none of the heavy guard dependency chain) only when it can prove every
guard is a no-op:

* the payload is not a mapping (all guards no-op on a non-dict payload); or
* NEITHER ``<cwd>/.hpc`` NOR the journal home directory exists.

Soundness: ``<cwd>/.hpc`` is the root of the skill-return ``_returns`` scan and
the relay-audit ``notebooks`` scan; the journal home
(``HPC_JOURNAL_DIR`` or ``~/.claude/hpc``) is the root of every run journal (the
decision-rendezvous + relay-audit ``runs`` predicates) AND of the skill-return
breadcrumb. If neither exists there is no state any guard could read, so each
guard is individually a no-op — the necessary-condition equivalence the contract
test pins. The check is a COARSE existence gate, never the guards' real logic: the
guards still own (and re-resolve) the exact per-repo paths.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Any

__all__ = [
    "compose_output",
    "dispatch",
    "main",
    "prefilter_should_run",
    "read_stdin_payload",
]

# The default guard dispatch order when the command carries no explicit module
# args (a bare ``python -m …stop_multiplex``). The installed command names these
# explicitly so the fused entry's command string still carries each legacy needle
# for capability detection; this constant is the defensive fallback and the ONE
# in-code statement of the order.
_DEFAULT_GUARDS: tuple[str, ...] = (
    "hpc_agent._kernel.hooks.skill_return_stop_guard",
    "hpc_agent._kernel.hooks.decision_rendezvous_stop_guard",
    "hpc_agent._kernel.hooks.relay_audit_stop",
)


def read_stdin_payload() -> Any:
    """Read the Stop payload from ``sys.stdin.buffer`` — robust, never crashes.

    Reads raw bytes and decodes utf-8 with ``errors="replace"`` (fail toward
    running: a non-utf8 byte becomes a replacement char rather than raising), then
    parses JSON. Returns the parsed value (normally a ``dict``), or ``None`` for an
    empty / unreadable / non-JSON payload. Any I/O or decode failure degrades to
    ``None`` — the caller then treats it as "no payload", and every guard no-ops.

    Shared by the fused Stop hook and the ``UserPromptSubmit`` capture shim
    (:mod:`hpc_agent._kernel.hooks.utterance_capture`) so there is ONE robust
    payload reader across the hook surface.
    """
    try:
        raw = sys.stdin.buffer.read()
        text = raw.decode("utf-8", errors="replace")
    except (OSError, ValueError, AttributeError):
        # AttributeError guards a stdin with no ``.buffer`` (e.g. a text-only
        # stub in a test harness) — fall back to the text read. Any failure on
        # that path degrades to "no payload" (fail toward running).
        try:
            text = sys.stdin.read()
        except Exception:
            return None

    if not text.strip():
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _journal_home() -> str:
    """The journal home directory, resolved stdlib-only (no ``hpc_agent`` import).

    ``HPC_JOURNAL_DIR`` (non-empty) wins, else ``~/.claude/hpc`` — the DEFAULT
    branch of :func:`hpc_agent.state.run_record.current_homedir`, restated here as
    a coarse existence gate ONLY. Returns a plain ``str`` path (``os.path``, not
    ``pathlib``) so the dry no-op hook path imports no ``pathlib`` (latency unit
    1.7 / hook-floor unit). This is a path, not a guard predicate: the prefilter
    merely asks "does the journal home exist at all"; the guards own the per-repo
    ``<home>/<repo_hash>/…`` resolution. (The prefilter deliberately does NOT
    honour the ``run_record.HPC_HOMEDIR`` monkeypatch attribute — an hpc_agent-side
    back-compat seam it must not import; missing that only makes the prefilter MORE
    conservative, i.e. it runs the guards, never wrongly skips.)
    """
    env_val = os.environ.get("HPC_JOURNAL_DIR")
    if env_val:
        return env_val
    return os.path.join(os.path.expanduser("~"), ".claude", "hpc")


def prefilter_should_run(payload: Any) -> bool:
    """Necessary-condition prefilter: may any Stop guard do work? (stdlib-only).

    Returns ``False`` (skip — the fused hook exits without importing the heavy
    guard chain) ONLY when it can prove every guard is a no-op:

    * *payload* is not a mapping; or
    * neither ``<cwd>/.hpc`` nor the journal home
      (:func:`_journal_home`) exists.

    Otherwise returns ``True`` (run all guards). Conservative by construction — any
    doubt (a filesystem error, an existing state root) biases to running. See the
    module docstring for the soundness argument.
    """
    if not isinstance(payload, dict):
        return False

    cwd = payload.get("cwd")
    cwd_dir = cwd if isinstance(cwd, str) and cwd else os.getcwd()

    try:
        if os.path.exists(os.path.join(cwd_dir, ".hpc")):
            return True
    except OSError:
        return True  # can't stat → run (fail toward running)

    try:
        if os.path.exists(_journal_home()):
            return True
    except OSError:
        return True

    return False


def dispatch(payload: Any, guard_modules: tuple[str, ...]) -> list[Any]:
    """Run each guard's ``build_hook_output`` on *payload*, isolated, in order.

    Imports each module in *guard_modules* and calls its ``build_hook_output``,
    collecting the per-guard result (``dict`` or ``None``) in dispatch order. Each
    guard runs in its OWN ``try`` so one guard's import/attribute/runtime failure
    contributes ``None`` and never stops the others — and never suppresses another
    guard's accounting side-effects, which happen inside ``build_hook_output``.

    Returns the list of per-guard outputs (same length + order as *guard_modules*),
    ``None`` for a guard that produced nothing or failed.
    """
    outputs: list[Any] = []
    for module_path in guard_modules:
        try:
            module = importlib.import_module(module_path)
            build = getattr(module, "build_hook_output", None)
            outputs.append(build(payload) if callable(build) else None)
        except Exception:
            outputs.append(None)
    return outputs


def compose_output(outputs: list[Any]) -> dict[str, Any] | None:
    """Compose per-guard outputs into ONE Stop hook-output dict, or ``None``.

    * **First-block-wins**: the first guard (in dispatch order) whose output is a
      ``{"decision": "block", …}`` supplies the ``decision`` + ``reason``.
    * **systemMessages accumulate**: every guard's non-empty ``systemMessage`` is
      carried, joined in dispatch order — a guard's audit/proceed note survives
      even when a different guard supplies the block.

    Returns ``None`` when no guard blocked and none carried a ``systemMessage`` (→
    the caller prints nothing and the stop proceeds).
    """
    system_messages: list[str] = []
    block: dict[str, Any] | None = None
    for out in outputs:
        if not isinstance(out, dict):
            continue
        sm = out.get("systemMessage")
        if isinstance(sm, str) and sm:
            system_messages.append(sm)
        if block is None and out.get("decision") == "block":
            block = out

    result: dict[str, Any] = {}
    if system_messages:
        result["systemMessage"] = "\n\n".join(system_messages)
    if block is not None:
        result["decision"] = "block"
        reason = block.get("reason")
        if reason is not None:
            result["reason"] = reason
    return result or None


def _guard_modules(argv: list[str] | None) -> tuple[str, ...]:
    """The guard module paths to dispatch — the CLI args, or :data:`_DEFAULT_GUARDS`."""
    args = tuple(argv if argv is not None else sys.argv[1:])
    return args or _DEFAULT_GUARDS


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, dispatch, maybe print.

    Reads the Stop payload once (:func:`read_stdin_payload`). If the syntactic
    prefilter proves no guard can do work, returns ``0`` immediately WITHOUT
    importing any guard (the heavy ``hpc_agent`` dependency chain stays unloaded).
    Otherwise dispatches every guard on the shared payload
    (:func:`dispatch`), composes their outputs (:func:`compose_output`), and prints
    the resulting JSON when non-``None``. Never raises, never exits non-zero — a
    broken fused hook degrades to today's behaviour (the stop proceeds), never
    wedges the harness.
    """
    payload = read_stdin_payload()

    if not prefilter_should_run(payload):
        return 0

    outputs = dispatch(payload, _guard_modules(argv))
    output = compose_output(outputs)
    if output is not None:
        print(json.dumps(output), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the harness / -m
    raise SystemExit(main())
