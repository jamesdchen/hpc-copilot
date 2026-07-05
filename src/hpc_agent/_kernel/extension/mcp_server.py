"""Model Context Protocol (MCP) server — a registry-projected surface.

This is the fourth agent surface (after slash commands, workflow skills,
and worker prompts): an MCP server that speaks JSON-RPC 2.0 over stdio.
It is an **additive projection** of the ``@primitive`` registry, not a
re-implementation of the CLI — the same design ``docs/reference/agent-surface.md``
anticipated ("MCP wrappers can be written on top of the CLI"). Two halves:

* **Discovery is projected.** ``tools/list`` walks the live registry
  (:func:`hpc_agent._kernel.registry.primitive.get_registry`) exactly like
  ``cli/parser.py`` walks it to build argparse. There is no second
  hand-maintained tool list, so the MCP surface cannot drift from the CLI —
  the CLI registry stays the single source of truth.
* **Invocation drives the CLI dispatch path — in-process by default.**
  ``tools/call`` writes the spec to a temp file and drives the SAME
  ``cli.dispatch.main`` code path the ``hpc-agent`` binary runs, then maps
  the JSON envelope back. The default runner is
  :func:`_in_process_cli_runner` (warm registry, ~40 ms/call vs ~1.2 s for a
  subprocess — measured 2026-07-04); :func:`_subprocess_cli_runner` stays
  injectable as the isolation fallback and the parity oracle. Every contract
  the CLI carries — exit-code → category, schema validation,
  journal/idempotency, the ``{ok, error_code, category, retry_safe,
  remediation}`` failure shape — is inherited verbatim, with zero
  re-implementation.

Safety posture (the reason this ships)
=======================================

The headless worker fence (:data:`hpc_agent._kernel.lifecycle.invoke._CLUSTER_OP_DENY_COMMANDS`)
re-imposes a no-``scancel`` / no-``ssh`` / no-exfil deny on three CLI config
surfaces because a shell-bearing worker *could* run those commands. An MCP
client has no shell — it can only call the verbs this server exposes. So the
deny boundary collapses to "which verbs are registered as tools", and:

* **Read-only by default.** Only ``query`` / ``validate`` primitives are
  exposed. Mutating verbs (``mutate`` / ``submit`` / ``workflow`` /
  ``scaffold``) require an explicit opt-in (``allow_mutations`` /
  ``--allow-mutations``).
* **No cancel/raw-submit verb exists in the registry at all** — ``scancel`` /
  ``qdel`` / ``sbatch`` / ``qsub`` are never hpc-agent primitives, so they are
  structurally unreachable through this surface regardless of the flag.
  ``tests/test_mcp_server.py`` pins this invariant.

Addressing the trade-offs MCP introduces
=========================================

* **Failure contract.** MCP collapses results into ``{content, isError}``;
  the CLI's machine-readable ``error_code`` / ``category`` / ``retry_safe`` and
  the exit code would be lost. We keep them: the full envelope (plus the
  process ``exit_code``) rides in ``structuredContent``, and ``isError`` is set
  from ``ok``/exit-code. A client that reads ``structuredContent`` recovers the
  same semantics it had over the CLI.
* **Context bloat vs. tiered discovery.** The CLI deliberately offers
  ``find`` → ``describe`` → invoke so a headless loop never dumps the whole
  catalog. ``catalog="tiered"`` mirrors that: it advertises only ``find`` +
  ``describe`` + a generic ``run-primitive`` tool, so the per-tool schemas of
  every primitive stay out of the model's context until pulled on demand.
  ``catalog="full"`` (default) exposes each read-only primitive as its own
  typed tool.
* **Version skew.** The server's package version is reported in
  ``serverInfo.version`` and called out in ``initialize`` ``instructions`` so a
  client can detect a daemon/package mismatch.

The protocol layer (JSON-RPC framing, dispatch) and the projection layer are
kept as pure functions / a plain class so both are unit-testable without a real
stdio transport: :class:`McpServer.handle` takes a request dict and returns a
response dict, and the subprocess runner is injectable.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.extension.workflow_entries import WORKFLOW_ENTRIES_BY_PROMPT
from hpc_agent.cli._dispatch import CliShape, _leaf_verb

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import IO

    from hpc_agent._kernel.registry.primitive import PrimitiveMeta

# MCP protocol revision this server is written against. On ``initialize`` we
# echo back the client's requested version when it sends one (the broadest
# compatibility posture for a minimal server) and fall back to this otherwise.
_PROTOCOL_VERSION = "2025-06-18"

# The read/act boundary, mirrored from the @primitive ``verb`` taxonomy
# (docs/architecture.md → "The decide / act boundary"). Only these are exposed
# without an explicit mutation opt-in.
_READ_ONLY_VERBS = frozenset({"query", "validate"})
_MUTATING_VERBS = frozenset({"mutate", "submit", "scaffold", "workflow"})

# The synthetic tool that preserves CLI-style tiered discovery under MCP.
_RUN_PRIMITIVE_TOOL = "run-primitive"

# The ``curated`` catalog's fixed extras: the recovery verbs (``doctor`` detects
# stalled/orphaned runs, ``kill`` the request→confirm kill, ``net-triage`` the
# bounded connectivity differential — the 2026-07-05 incident's missing tool)
# plus the ``submit-speculate`` opt-in touchpoint — AND the human-amplification
# LOOP surfaces the block verbs are driven through: ``block-drive`` (the chain
# driver the skills invoke — one stateless tick that parks or detaches, so it is
# MCP-safe, never a multi-hour blocking call) and ``append-decision`` (the
# greenlight commit). Without these two, an MCP-first agent reaches the block
# verbs but not the DRIVER or the COMMIT, so it drops to raw CLI for the core
# loop and hand-authors specs — reintroducing the finding-13/17 corruption class
# (run #6). These are stable human-amplification surfaces that are NOT blocks
# (their Result models carry no ``next_block``), so they are unioned in
# explicitly. Everything else in ``curated`` is DERIVED (see
# :func:`_declares_next_block`) — a block is any verb whose Result model declares
# a ``next_block`` field, so adding/removing that field moves a verb in/out of
# the curated set with no edit here.
_CURATED_EXTRA_VERBS = frozenset(
    {"doctor", "kill", "net-triage", "submit-speculate", "block-drive", "append-decision"}
)

# Read-only context resources, each backed by a CLI verb. The URI scheme is
# informational; the value is the argv driven through the same runner as tools.
_RESOURCES: dict[str, tuple[tuple[str, ...], str]] = {
    "hpc-agent://capabilities": (
        ("capabilities",),
        "Operations catalog + environment metadata (mirror of `hpc-agent capabilities`).",
    ),
    "hpc-agent://clusters": (
        ("clusters", "list"),
        "Configured cluster definitions (mirror of `hpc-agent clusters list`).",
    ),
}

# The four user-facing workflow slash commands, surfaced as MCP prompts.
# PROJECTED, not hand-listed (§6): the name set and each entry's
# start-the-driver instruction are sourced from the single workflow-entry table
# (:mod:`hpc_agent._kernel.extension.workflow_entries`) — the one canonical
# source the Claude-Code slash AND this MCP prompt both project from. Adding a
# workflow there surfaces it here with no edit; ``get_prompt`` still reads the
# packaged command ``.md`` for the human description body.
_PROMPT_NAMES = tuple(WORKFLOW_ENTRIES_BY_PROMPT)


# A runner takes an hpc-agent argv (without the leading binary) and returns
# ``(exit_code, stdout, stderr)``. Injected for testability.
CliRunner = Callable[["list[str]"], "tuple[int, str, str]"]


# Server-level cap on one isolated-runner tool call. Generous: the blocking
# watch/wait invocations are refused at the seam (conduct rule 11 —
# :func:`_refuse_blocking_over_mcp` requires detach for submit-s2/s3 and rejects
# blocking status-watch polls), so a legitimate call is minutes, not hours; 1 h
# still covers a slow stage/harvest with wide margin while guaranteeing the
# wedge class (an unbounded piped wait) cannot recur through this runner.
_SUBPROCESS_RUNNER_TIMEOUT_SEC: float = 3600.0


def _isolated_runner_argv(argv: list[str]) -> list[str]:
    """The argv the isolated runner executes (a seam so tests can substitute)."""
    return [sys.executable, "-m", "hpc_agent", *argv]


def _subprocess_cli_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run ``python -m hpc_agent <argv...>`` in a SUBPROCESS and capture its envelope.

    ``sys.executable -m hpc_agent`` rather than a bare ``hpc-agent`` on PATH so
    the server drives the *same* interpreter/install it was launched from —
    no dependency on the console script being on PATH, and no version skew
    between the registry this process projected and the binary it invokes.

    This is the fully-isolated runner (its own process, its own cwd/globals). It
    is kept available for tests and callers that want process isolation, but the
    server DEFAULTS to :func:`_in_process_cli_runner` for latency (no per-call
    interpreter cold-start + registry walk). The two are parity-checked
    (``tests/test_mcp_server.py``): the same tool call through either yields an
    identical envelope + exit code.

    The wait is BOUNDED by :data:`_SUBPROCESS_RUNNER_TIMEOUT_SEC`, and the
    piped capture routes through ``infra.remote._capture_via_select`` — the
    S2-wedge-fix seam whose deadline can actually fire on Windows (kill on
    expiry, bounded post-kill drain). On expiry the child is killed and the
    call maps to exit 124 (the ``timeout(1)`` convention) with the deadline
    named on stderr.
    """
    from hpc_agent.infra.remote import _capture_via_select

    try:
        proc = _capture_via_select(
            _isolated_runner_argv(argv), timeout=_SUBPROCESS_RUNNER_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        return (
            124,
            "",
            (
                f"hpc-agent {' '.join(argv)} exceeded the isolated runner's "
                f"{_SUBPROCESS_RUNNER_TIMEOUT_SEC:.0f}s deadline — child killed"
            ),
        )
    return proc.returncode, proc.stdout, proc.stderr


def _in_process_cli_runner(argv: list[str]) -> tuple[int, str, str]:
    """Dispatch ``hpc-agent <argv...>`` IN-PROCESS via :func:`cli.dispatch.main`.

    The server process already holds the warm ``@primitive`` registry it
    projected; the subprocess runner re-paid Python cold-start + the full
    registry walk on every single tool call. This runner reuses the imported
    registry (``register_primitives`` is idempotent) and drives the SAME
    ``cli.dispatch.main(argv)`` code path, so the ``(exit_code, stdout-envelope,
    stderr)`` contract — and therefore the ``error_code`` / ``category`` /
    ``retry_safe`` / ``exit_code`` the envelope carries — is reproduced exactly,
    just without a subprocess.

    Parity mechanics: ``main`` prints the single-line JSON envelope to stdout and
    diagnostics to stderr and returns the int exit code; we capture both streams
    and the return value. ``argparse`` error paths raise ``SystemExit`` (its code
    is the exit code, ``None`` → 0); any other uncaught exception is mapped to
    exit code 1 with the traceback on stderr, matching a subprocess whose Python
    died on an uncaught exception. ``_subprocess_cli_runner`` remains the
    isolated fallback for tests / callers that want a separate process.

    State-leak audit (§7)
    ---------------------
    This runner reuses the process's module-global ``@primitive`` registry
    across every tool call, so cross-call global-state leakage was audited. It
    is CLEAN, for structural reasons:

    * **The registry is populated idempotently, never mutated per call.**
      ``register_primitives()`` is idempotent and runs once at ``build_server``;
      ``main``/verb dispatch only *reads* it. No verb registers, edits, or
      unregisters a primitive at invocation, so the shared registry cannot drift
      between calls.
    * **Per-call state is local, not module-global.** ``argv``, the captured
      ``StringIO`` stdout/stderr, and ``code`` are all locals rebound every call;
      nothing is stashed on a module attribute or default-argument object.
    * **stdout/stderr are restored deterministically.**
      ``contextlib.redirect_stdout``/``redirect_stderr`` are context managers
      that restore the real streams on exit — including on the ``SystemExit`` /
      broad-``Exception`` paths (the ``with`` unwinds before the ``except``
      body runs), so a crashing verb cannot leave the process's streams
      redirected into a dead ``StringIO`` for the next call.
    * **Durable state (journal / filesystem / cwd) is the CLI's own contract**,
      identical whether the verb runs in-process or in a subprocess — it is
      *intended* cross-call state (the decision journal is how blocks chain),
      not a leak. The subprocess runner shares it too.

    Net: the only state shared between calls is the read-only registry and the
    intended durable journal/filesystem; both are the same under the subprocess
    runner, which is why the parity test holds across a mutating + a workflow
    verb, not just ``find``.
    """
    import io
    import traceback

    from hpc_agent.cli.dispatch import main as _cli_main

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = _cli_main(list(argv))
    except SystemExit as exc:  # argparse / explicit sys.exit inside a verb
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception:  # noqa: BLE001 — parity: an uncaught crash is exit 1 + traceback on stderr
        err.write(traceback.format_exc())
        code = 1
    return int(code), out.getvalue(), err.getvalue()


# Conduct rule 11 (proving-run-3 finding (h)): the in-process dispatch is
# SYNCHRONOUS and single-threaded, so one blocking tools/call (a canary/array
# watch, a wait-to-terminal poll) wedges the whole server and every later call
# queues behind it — observed twice in run #3 (26-min and 20-min head-of-line
# stalls; an abandoned agent turn does NOT kill the server-side call). The
# blocking invocations are refused HERE, at the seam, with the detached path
# named — never left to skill prose.
_DETACH_REQUIRED_VERBS = frozenset({"submit-s2", "submit-s3"})


def _refuse_blocking_over_mcp(name: str, arguments: Mapping[str, Any]) -> None:
    """Raise ``_Invalid`` for tool calls that would block the server.

    ``submit-s2``/``submit-s3`` must carry ``spec.detach == true`` (the
    detached worker + ``wait-detached`` is the sanctioned wait);
    ``status-watch`` must not request a blocking wait-to-terminal poll.
    """
    spec = arguments.get("spec")
    spec_dict = spec if isinstance(spec, dict) else {}
    if name in _DETACH_REQUIRED_VERBS and not spec_dict.get("detach"):
        raise _Invalid(
            f"{name} without detach=true is a BLOCKING scheduler watch — over "
            "this synchronous server it wedges every later tool call "
            "(head-of-line; an abandoned turn does not stop it). Set "
            '{"detach": true} in the spec; the block returns a pid handle '
            "immediately. Then run `hpc-agent wait-detached` via backgrounded "
            "Bash to be woken when the brief is ready."
        )
    if name == "status-watch" and spec_dict.get("wait_terminal"):
        raise _Invalid(
            "status-watch with wait_terminal=true is a BLOCKING poll — it "
            "wedges this synchronous server. Invoke it without wait_terminal "
            "(detached-by-contract) and await the worker with `hpc-agent "
            "wait-detached` via backgrounded Bash."
        )


class _Invalid(Exception):
    """Maps to JSON-RPC ``-32602`` (invalid params) — a client contract error."""


class _MethodNotFound(Exception):
    """Maps to JSON-RPC ``-32601`` (method not found)."""


# ─── schema / projection helpers (pure) ────────────────────────────────────


def _load_input_schema(basename: str | None) -> dict[str, Any] | None:
    """Return the packaged ``schemas/<basename>.input.json`` as a dict, or None."""
    if not basename:
        return None
    try:
        from importlib.resources import files

        text = (files("hpc_agent.schemas") / f"{basename}.input.json").read_text(encoding="utf-8")
        loaded = json.loads(text)
    except (FileNotFoundError, ModuleNotFoundError, OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _arg_property(arg: Any) -> dict[str, Any]:
    """JSON-Schema property for one :class:`hpc_agent.cli._dispatch.CliArg`."""
    if arg.action in ("store_true", "store_false"):
        prop: dict[str, Any] = {"type": "boolean"}
    elif arg.type is int:
        prop = {"type": "integer"}
    else:
        prop = {"type": "string"}
    if arg.choices:
        prop["enum"] = list(arg.choices)
    if arg.help:
        prop["description"] = arg.help
    return prop


def _tool_input_schema(name: str, shape: CliShape) -> dict[str, Any]:
    """Build the MCP ``inputSchema`` for a primitive from its :class:`CliShape`.

    ``--spec`` primitives embed the packaged wire schema under a ``spec``
    object property (so a client gets the full typed contract); each
    ``CliArg`` becomes a property; ``experiment_dir`` is optional.
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    if shape.experiment_dir_arg:
        props["experiment_dir"] = {
            "type": "string",
            "description": "Path to the experiment repo (default: the server's working directory).",
        }
    if shape.spec_arg:
        spec_schema = _load_input_schema(shape.schema_ref.input if shape.schema_ref else None)
        props["spec"] = spec_schema or {"type": "object", "description": "JSON spec object."}
        if shape.spec_required:
            required.append("spec")
    for arg in shape.args:
        key = arg.attr_name()
        props[key] = _arg_property(arg)
        # Positional args (flag without a leading dash) are always required by
        # argparse; a flag is required only when it declares so.
        if not arg.flag.startswith("-") or arg.required:
            required.append(key)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    # The spec sub-object may legitimately carry extra keys; only the top
    # level is closed.
    schema["additionalProperties"] = False
    return schema


def _declares_next_block(meta: PrimitiveMeta) -> bool:
    """True when *meta*'s primitive returns a Result model declaring ``next_block``.

    The structural definition of a **block** (design §2/§3): a verb whose Result
    model carries the machine-computed ``next_block`` field. Derived — never a
    hardcoded verb list — so the curated MCP catalog tracks exactly the block
    verbs N marked, and adding/removing a ``next_block`` field on a Result model
    moves the verb in/out of the curated set automatically.

    Resolution is deliberately narrow to stay robust under ``from __future__
    import annotations``: the return annotation is a bare model NAME string
    (e.g. ``"SubmitBlockResult"``) resolved ONLY against the function's own
    module globals — NEVER ``typing.get_type_hints`` over the whole signature,
    which would choke on ``TYPE_CHECKING``-only param annotations (``Path``) that
    the block funcs use. A non-name return (``dict[str, Any]``) resolves to None
    → not a block.
    """
    func = meta.func
    ret = getattr(func, "__annotations__", {}).get("return")
    model: Any = None
    if isinstance(ret, str):
        model = getattr(func, "__globals__", {}).get(ret)
    elif isinstance(ret, type):
        model = ret
    fields = getattr(model, "model_fields", None)
    return isinstance(fields, dict) and "next_block" in fields


def _tool_definition(name: str, meta: PrimitiveMeta) -> dict[str, Any]:
    """Project one primitive into an MCP tool definition."""
    shape = meta.cli
    assert isinstance(shape, CliShape)  # callers filter on this
    return {
        "name": name,
        "title": name,
        "description": (meta.description or shape.help or name).strip(),
        "inputSchema": _tool_input_schema(name, shape),
        "annotations": {
            "title": name,
            "readOnlyHint": meta.verb in _READ_ONLY_VERBS,
            "destructiveHint": meta.verb in ("mutate", "submit"),
            "idempotentHint": bool(meta.idempotent),
        },
    }


def allowed_primitives(
    registry: Mapping[str, PrimitiveMeta], *, allow_mutations: bool
) -> dict[str, PrimitiveMeta]:
    """The primitives this server may expose/invoke, gated by the safety policy.

    Always includes ``query`` / ``validate`` primitives that have a CLI shape.
    Includes mutating verbs only when *allow_mutations* is set. This is the one
    place the read/act boundary is enforced; both catalog modes and the
    ``tools/call`` guard consult it, so a tiered-mode ``run-primitive`` can
    never reach a verb the policy forbids.
    """
    out: dict[str, PrimitiveMeta] = {}
    for prim_name, meta in registry.items():
        if not isinstance(meta.cli, CliShape):
            continue
        if meta.verb in _READ_ONLY_VERBS or (allow_mutations and meta.verb in _MUTATING_VERBS):
            out[prim_name] = meta
    return out


def _build_invocation(
    name: str, shape: CliShape, arguments: Mapping[str, Any], spec_path: str | None
) -> list[str]:
    """Render the ``hpc-agent`` argv for a tool call (binary name excluded)."""
    argv: list[str] = []
    if shape.group:
        argv.append(shape.group)
    argv.append(_leaf_verb(name, shape))

    positionals: list[str] = []
    optionals: list[str] = []
    for arg in shape.args:
        key = arg.attr_name()
        if key not in arguments or arguments[key] is None:
            continue
        value = arguments[key]
        if not arg.flag.startswith("-"):
            positionals.append(str(value))
        elif arg.action in ("store_true", "store_false"):
            if value:
                optionals.append(arg.flag)
        else:
            optionals.extend([arg.flag, str(value)])

    if shape.experiment_dir_arg and arguments.get("experiment_dir"):
        optionals.extend(["--experiment-dir", str(arguments["experiment_dir"])])
    if shape.spec_arg and spec_path is not None:
        optionals.extend(["--spec", spec_path])

    return argv + positionals + optionals


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Parse the single-line JSON envelope from CLI stdout (last non-blank line)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _tool_result(exit_code: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Map a CLI ``(exit_code, stdout, stderr)`` into an MCP tool result.

    Preserves the CLI's machine-readable failure contract: the full envelope
    (``error_code`` / ``category`` / ``retry_safe`` / ``remediation`` / ``data``)
    plus the process ``exit_code`` ride in ``structuredContent``, and
    ``isError`` is true on a non-zero exit or an ``ok:false`` envelope.
    """
    envelope = _parse_envelope(stdout)
    ok = isinstance(envelope, dict) and envelope.get("ok") is True
    is_error = exit_code != 0 or not ok
    if isinstance(envelope, dict):
        structured = dict(envelope)
    else:
        structured = {
            "ok": False,
            "error_code": "internal",
            "category": "internal",
            "retry_safe": False,
            "message": "hpc-agent produced no parseable JSON envelope.",
            "raw_stdout": stdout[-2000:],
            "raw_stderr": stderr[-2000:],
        }
    structured["exit_code"] = exit_code
    return {
        "content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}],
        "structuredContent": structured,
        "isError": is_error,
    }


def _read_command_md(name: str) -> str | None:
    """Return the body of a bundled ``slash_commands/commands/<name>.md`` file."""
    try:
        from importlib.resources import files

        body = (files("slash_commands") / "commands" / f"{name}.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    return body


def _strip_frontmatter(body: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` YAML-ish frontmatter block from a markdown body.

    Returns ``(frontmatter_kv, remaining_body)``. The parse is intentionally
    minimal (``key: value`` lines only) — enough to lift a ``description:`` for
    the prompt listing without a YAML dependency.
    """
    if not body.startswith("---"):
        return {}, body
    close = body.find("\n---", 3)
    if close == -1:
        return {}, body
    front = body[3:close]
    rest = body[close + 4 :]
    kv: dict[str, str] = {}
    for line in front.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            kv[key.strip()] = val.strip().strip("'\"")
    return kv, rest.lstrip("\n")


# ─── the server ─────────────────────────────────────────────────────────────


class McpServer:
    """Projects the primitive registry as an MCP server over JSON-RPC 2.0."""

    def __init__(
        self,
        *,
        registry: Mapping[str, PrimitiveMeta],
        allow_mutations: bool = False,
        catalog: str = "full",
        runner: CliRunner | None = None,
    ) -> None:
        if catalog not in ("full", "tiered", "curated"):
            raise ValueError(f"catalog must be 'full', 'tiered', or 'curated', got {catalog!r}")
        self._registry = registry
        self._allow_mutations = allow_mutations
        self._catalog = catalog
        # Default to the WARM in-process runner (reuses this process's registry;
        # no per-call interpreter cold-start). ``_subprocess_cli_runner`` (full
        # process isolation) is no longer the default but stays injectable as the
        # isolation fallback + the parity oracle the in-process runner is checked
        # against (tests/test_mcp_curated.py).
        self._runner = runner or _in_process_cli_runner

    # -- projection ---------------------------------------------------------

    def _allowed(self) -> dict[str, PrimitiveMeta]:
        return allowed_primitives(self._registry, allow_mutations=self._allow_mutations)

    def _cli_primitives(self) -> dict[str, PrimitiveMeta]:
        """All primitives with a CLI shape, regardless of the mutation gate.

        The mutation-independent base for the ``curated`` catalog: curated is
        itself a deliberate allowlist (its block verbs are all inherently
        ``workflow``-typed), so it is NOT re-gated by ``--allow-mutations`` — see
        :meth:`_curated_metas`.
        """
        return {
            name: meta for name, meta in self._registry.items() if isinstance(meta.cli, CliShape)
        }

    def _curated_metas(self) -> dict[str, PrimitiveMeta]:
        """The curated catalog's verb→meta map: derived blocks ∪ the fixed extras.

        A block is any verb whose Result model declares ``next_block``
        (:func:`_declares_next_block`) — derived, not hardcoded — unioned with
        the stable recovery/opt-in extras (:data:`_CURATED_EXTRA_VERBS`).

        Computed off :meth:`_cli_primitives`, NOT :meth:`_allowed`: the
        ``--allow-mutations ∩ curated`` intersection was vestigial (design §7).
        Curated is already the boundary — an authored allowlist of inherently
        mutating block verbs — and the verb-level guards (greenlight gate, drift
        guard, idempotency) still fire at invocation regardless of the flag, so
        gating the *listing* on ``allow_mutations`` only mis-hid the read-only
        ``workflow`` blocks (``status-snapshot`` / ``aggregate-check`` /
        ``campaign-watch``). ``full``/``tiered`` stay gated by :meth:`_allowed`.
        """
        base = self._cli_primitives()
        names = {name for name, meta in base.items() if _declares_next_block(meta)}
        names |= {v for v in _CURATED_EXTRA_VERBS if v in base}
        return {name: base[name] for name in sorted(names)}

    def _curated_names(self) -> list[str]:
        """The curated catalog's verb set (keys of :meth:`_curated_metas`)."""
        return list(self._curated_metas())

    def _invocable(self) -> dict[str, PrimitiveMeta]:
        """Primitives this server may INVOKE, per the active catalog's boundary.

        ``curated`` is its own allowlist (design §7 — see :meth:`_curated_metas`),
        so its block verbs are callable regardless of ``--allow-mutations``; the
        verb-level guards enforce. ``full``/``tiered`` invoke only what the
        read/act policy (:meth:`_allowed`) permits. Keeping the *call* gate and
        the *listing* on the same set means curated never advertises a tool it
        would then refuse to run.
        """
        if self._catalog == "curated":
            return self._curated_metas()
        return self._allowed()

    def list_tools(self) -> list[dict[str, Any]]:
        allowed = self._allowed()
        if self._catalog == "curated":
            # A small, human-amplification-shaped surface: the block verbs
            # (derived from a ``next_block`` Result field) plus the recovery /
            # opt-in extras — each as its own typed tool. NOT gated by
            # ``allow_mutations`` (design §7 — curated is itself the allowlist).
            curated = self._curated_metas()
            return [_tool_definition(name, meta) for name, meta in curated.items()]
        if self._catalog == "tiered":
            # Mirror the CLI's find → describe → invoke discovery: advertise
            # only the explorers plus a generic invoker, keeping every
            # primitive's per-tool schema out of the model's context until
            # pulled on demand via `describe`.
            tools: list[dict[str, Any]] = []
            for explorer in ("find", "describe"):
                if explorer in allowed:
                    tools.append(_tool_definition(explorer, allowed[explorer]))
            tools.append(self._run_primitive_definition())
            return tools
        return [_tool_definition(name, meta) for name, meta in sorted(allowed.items())]

    def _run_primitive_definition(self) -> dict[str, Any]:
        verbs = "query/validate" if not self._allow_mutations else "any registered"
        return {
            "name": _RUN_PRIMITIVE_TOOL,
            "title": "run an hpc-agent primitive by name",
            "description": (
                "Invoke any exposed hpc-agent primitive by name. Use `find` to "
                "search the catalog and `describe <name>` to fetch a primitive's "
                f"contract first. Only {verbs} primitives are reachable."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The primitive's wire name."},
                    "arguments": {
                        "type": "object",
                        "description": 'The primitive\'s arguments (e.g. {"spec": {...}}).',
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": not self._allow_mutations},
        }

    # -- tools/call ---------------------------------------------------------

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == _RUN_PRIMITIVE_TOOL:
            inner = arguments.get("name")
            if not isinstance(inner, str):
                raise _Invalid(f"{_RUN_PRIMITIVE_TOOL} requires a string 'name'")
            inner_args = arguments.get("arguments") or {}
            if not isinstance(inner_args, dict):
                raise _Invalid(f"{_RUN_PRIMITIVE_TOOL} 'arguments' must be an object")
            return self.call_tool(inner, inner_args)

        _refuse_blocking_over_mcp(name, arguments)
        meta = self._invocable().get(name)
        if meta is None:
            # Either the primitive does not exist or the safety policy forbids
            # it (a mutating verb without ``allow_mutations`` under the
            # full/tiered catalogs; the curated catalog is its own allowlist and
            # does not gate on the flag). Both are client contract errors, not
            # tool failures.
            raise _Invalid(
                f"tool {name!r} is not available. It is either unknown or a "
                "mutating verb that requires the server to be started with "
                "--allow-mutations."
            )
        shape = meta.cli
        assert isinstance(shape, CliShape)

        spec_path: str | None = None
        spec = arguments.get("spec")
        try:
            if shape.spec_arg and spec is not None:
                fd, spec_path = tempfile.mkstemp(prefix="hpc-mcp-spec-", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(spec, fh)
            argv = _build_invocation(name, shape, arguments, spec_path)
            started = time.perf_counter()
            exit_code, stdout, stderr = self._runner(argv)
            # Per-call telemetry (2026-07-04): "why is MCP slow" must be a
            # measurement, not a mystery. One stderr line per tools/call —
            # stderr rides the harness's MCP log, never the JSON-RPC channel.
            sys.stderr.write(
                f"[mcp] {name} {int((time.perf_counter() - started) * 1000)}ms exit={exit_code}\n"
            )
        finally:
            if spec_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(spec_path)
        return _tool_result(exit_code, stdout, stderr)

    # -- resources ----------------------------------------------------------

    def list_resources(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": uri,
                "name": uri.rsplit("/", 1)[-1],
                "description": description,
                "mimeType": "application/json",
            }
            for uri, (_argv, description) in _RESOURCES.items()
        ]

    def read_resource(self, uri: Any) -> dict[str, Any]:
        entry = _RESOURCES.get(uri) if isinstance(uri, str) else None
        if entry is None:
            raise _Invalid(f"unknown resource uri {uri!r}")
        argv, _description = entry
        _exit_code, stdout, stderr = self._runner(list(argv))
        text = stdout.strip() or stderr.strip()
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}

    # -- prompts ------------------------------------------------------------

    def list_prompts(self) -> list[dict[str, Any]]:
        # The prompt SET is projected from the workflow-entry table (§6); the
        # human description body is still lifted from the packaged command .md,
        # falling back to the entry's start-the-driver instruction when the .md
        # is not installed — the table stays the source of truth either way.
        prompts: list[dict[str, Any]] = []
        for name in _PROMPT_NAMES:
            entry = WORKFLOW_ENTRIES_BY_PROMPT[name]
            body = _read_command_md(name)
            if body is None:
                desc = f"/{name} — start the {entry.name} workflow"
                prompts.append({"name": name, "description": desc})
                continue
            front, _rest = _strip_frontmatter(body)
            prompts.append({"name": name, "description": front.get("description", f"/{name}")})
        return prompts

    def get_prompt(self, name: Any, _arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in _PROMPT_NAMES:
            raise _Invalid(f"unknown prompt {name!r}")
        entry = WORKFLOW_ENTRIES_BY_PROMPT[str(name)]
        body = _read_command_md(str(name))
        if body is None:
            # No packaged slash body in this install — project the canonical
            # entry's thin start-the-driver instruction from the table instead
            # (§6: the MCP prompt is a projection of the same one source).
            return {
                "description": f"/{name} — start the {entry.name} workflow",
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": entry.start_instruction}}
                ],
            }
        front, rest = _strip_frontmatter(body)
        return {
            "description": front.get("description", f"/{name}"),
            "messages": [{"role": "user", "content": {"type": "text", "text": rest}}],
        }

    # -- JSON-RPC -----------------------------------------------------------

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if self._catalog == "tiered":
            catalog_note = (
                "Tiered catalog: use the `find` and `describe` tools to discover "
                "primitives, then `run-primitive` to invoke one."
            )
        elif self._catalog == "curated":
            catalog_note = (
                "Curated catalog: the human-amplification block verbs (each "
                "returns a next_block suggestion), the loop driver `block-drive` "
                "and the greenlight commit `append-decision`, plus the "
                "recovery/opt-in verbs (doctor, kill, net-triage, submit-speculate) "
                "are exposed as typed tools. Drive the submit/aggregate/campaign "
                "loops via `block-drive` and commit each `y` via `append-decision` "
                "— do not hand-author specs on the CLI."
            )
        else:
            catalog_note = "Each read-only primitive is exposed as its own typed tool."
        mutation_note = (
            "Mutating verbs (submit/aggregate/scaffold) ARE exposed."
            if self._allow_mutations
            else "Read-only (query/validate) verbs only; mutating verbs are gated off."
        )
        # Fingerprinted version (``0.10.65+g<sha>``): the version *number*
        # alone cannot express skew between installs of the same release, and
        # the instructions explicitly tell clients to compare versions — so
        # serverInfo carries the commit identity too. Lazy import; resolved
        # once per server (initialize), fail-open to the bare number.
        from hpc_agent._build_info import full_version

        server_version = full_version()
        return {
            "protocolVersion": requested if isinstance(requested, str) else _PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": "hpc-agent", "version": server_version},
            "instructions": (
                f"hpc-agent {server_version} MCP server. Tools mirror the "
                "`hpc-agent` CLI registry (`hpc-agent capabilities`); each tool "
                "result carries the full CLI envelope in structuredContent "
                "(error_code, category, retry_safe, exit_code). "
                f"{mutation_note} {catalog_note} Compare serverInfo.version against "
                "your client's expected hpc-agent version to detect skew."
            ),
        }

    def _dispatch(self, method: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise _Invalid("tools/call requires a string 'name'")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _Invalid("tools/call 'arguments' must be an object")
            return self.call_tool(name, arguments)
        if method == "resources/list":
            return {"resources": self.list_resources()}
        if method == "resources/read":
            return self.read_resource(params.get("uri"))
        if method == "prompts/list":
            return {"prompts": self.list_prompts()}
        if method == "prompts/get":
            return self.get_prompt(params.get("name"), params.get("arguments") or {})
        raise _MethodNotFound()

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def handle(self, request: Any) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC request; return its response, or None for a notification."""
        if not isinstance(request, dict):
            return self._error(None, -32600, "invalid request")
        is_notification = "id" not in request
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return None if is_notification else self._error(req_id, -32602, "params must be object")
        try:
            result = self._dispatch(method, params)
        except _Invalid as exc:
            return None if is_notification else self._error(req_id, -32602, str(exc))
        except _MethodNotFound:
            if is_notification:
                return None
            return self._error(req_id, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001 — any handler bug becomes a JSON-RPC error, not a crash
            if is_notification:
                return None
            return self._error(req_id, -32603, f"internal error: {exc}")
        return None if is_notification else {"jsonrpc": "2.0", "id": req_id, "result": result}

    def serve(self, stdin: IO[str], stdout: IO[str]) -> None:
        """Run the newline-delimited JSON-RPC loop until stdin closes."""
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._write(stdout, self._error(None, -32700, "parse error"))
                continue
            response = self.handle(request)
            if response is not None:
                self._write(stdout, response)

    @staticmethod
    def _write(stdout: IO[str], message: dict[str, Any]) -> None:
        stdout.write(json.dumps(message) + "\n")
        stdout.flush()


def build_server(
    *, allow_mutations: bool = False, catalog: str = "full", runner: CliRunner | None = None
) -> McpServer:
    """Construct an :class:`McpServer` over the live primitive registry.

    Calls :func:`register_primitives` (idempotent) so the registry is populated
    before it is projected.
    """
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    return McpServer(
        registry=get_registry(),
        allow_mutations=allow_mutations,
        catalog=catalog,
        runner=runner,
    )


__all__ = [
    "CliRunner",
    "McpServer",
    "allowed_primitives",
    "build_server",
]
