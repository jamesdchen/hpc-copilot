"""``PostToolUse`` hook — auto-fetch a sub-skill's return envelope.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.PostToolUse``
array (see :func:`hpc_agent.agent_assets.install_agent_assets`). It is invoked
by the harness after every matched tool call, receives the PostToolUse payload
as JSON on **stdin**, and may emit a JSON object on **stdout** to inject
context into the agent's next observation.

Why it exists
-------------
A parent skill that composes ``Skill(<sub>)`` must, by prose discipline, chain
``hpc-agent fetch-skill-return --skill <sub>`` to read the sub-skill's return
envelope from ``<experiment_dir>/.hpc/_returns/<skill>.json`` (see
:mod:`hpc_agent.cli.skill_returns`). That manual follow-up is one of two seams
where the parent's prose-discipline still matters. This hook removes it: the
moment the sub-skill's final ``hpc-agent emit-skill-return`` Bash call commits
the envelope, the hook reads it back and injects it as ``additionalContext`` so
the envelope is in the agent's next observation whether or not the parent
remembers to fetch it.

Why it fires on ``Bash``/``emit-skill-return``, not on ``Skill``
----------------------------------------------------------------
The pre-0.10.58 version matched the ``Skill`` tool, on the assumption that
"the ``Skill(<sub>)`` call returned" meant "the sub-skill finished". It does
not: Claude Code's ``Skill`` tool returns *immediately* — its tool result is
the injected skill instructions, and the sub-skill's steps (including the
final ``emit-skill-return``) run **afterwards** as ordinary tool calls in the
same conversation. At ``PostToolUse(Skill)`` time the envelope cannot exist
yet, so the hook was a structural no-op on every fresh run (and could only
ever inject a *stale* envelope left over from a prior run). The corrected
trigger is the one event that coincides with the envelope existing: the
``Bash`` call whose command invokes ``emit-skill-return`` for a known skill.
(Empirical: 2026-06-10 demo, ``hpc-wrap-entry-point`` emitted its return and
the turn still ended with the envelope unfetched — the "net" never fired.)

Contract & defensiveness
------------------------
The hook is a **pure, additive, fail-open** observer:

* It only acts when the just-completed tool is ``Bash``, its command invokes
  ``emit-skill-return`` with a ``--skill`` in
  :data:`hpc_agent.cli.skill_returns._KNOWN_SKILLS`.
* For any other tool, an unknown/unresolvable skill, a missing or malformed
  return file, or a malformed payload, it is a **clean no-op** — it prints
  nothing and exits ``0``. It never raises and never exits non-zero, so it can
  never block a tool call or crash the harness. A failed emit (validation
  refusal) leaves no committed file, so it degrades to the same no-op.
* It does **not** delete the return file (unlike the manual
  ``fetch-skill-return``, which clears by default). Leaving the file on disk
  keeps the existing parent-side ``fetch-skill-return`` prose working
  unchanged — the injection is purely additive context, not a replacement for
  the documented seam.

The ``experiment_dir`` is taken from the emit command's own
``--experiment-dir`` flag when present (the authoritative location the emitter
wrote to), falling back to the payload's ``cwd`` and finally
:func:`os.getcwd`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Single source of truth for the set of sub-skills that emit a return envelope.
# Re-exported from the CLI primitive so this hook and the verbs can never drift.
from hpc_agent.cli.skill_returns import _KNOWN_SKILLS, _committed_path

__all__ = [
    "build_hook_output",
    "extract_emit_invocation",
    "main",
    "read_committed_envelope",
]


def read_committed_envelope(experiment_dir: Path, skill: str) -> str | None:
    """The committed sub-skill return envelope as canonical JSON, or ``None``.

    The ONE reader both the PostToolUse autofetch and the Stop-guard completer
    (:mod:`hpc_agent._kernel.hooks.skill_return_stop_guard`) route through — the
    same bytes ``fetch-skill-return`` would print to stdout (``json.dumps(...,
    sort_keys=True)``). A missing / permission-denied file (the emit itself
    failed validation) or non-JSON content yields ``None`` (the caller stays a
    clean no-op). Read-only: it never clears the file — the autofetch is purely
    additive, and the completer clears explicitly once it has injected.
    """
    committed = _committed_path(experiment_dir, skill)
    try:
        envelope_text = committed.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        envelope = json.loads(envelope_text)
    except (json.JSONDecodeError, ValueError):
        return None
    return json.dumps(envelope, sort_keys=True)


# The emit must be an ACTUAL ``hpc-agent emit-skill-return`` invocation, not a
# mere mention of the substring — a read-only ``grep -- emit-skill-return
# --skill <name> notes.md`` used to false-positive (finding #56). Anchoring on
# the ``hpc-agent emit-skill-return`` command token pair rejects the mention.
_EMIT_INVOCATION_RE = re.compile(r"hpc-agent\s+emit-skill-return\b")
# ``--skill <name>`` / ``--skill=<name>`` (optionally quoted) inside the Bash
# command. The name charset mirrors skill_returns._SKILL_NAME_RE.
_SKILL_FLAG_RE = re.compile(r"--skill(?:=|\s+)['\"]?([a-z][a-z0-9-]*)")
# ``--experiment-dir <path>`` — double-quoted, single-quoted, or a bare token
# (stopping at whitespace and shell metacharacters so a chained ``&& next``
# or a closing paren is not swallowed into the path).
_EXPERIMENT_DIR_FLAG_RE = re.compile(
    r"--experiment-dir(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|)]+))"
)


def extract_emit_invocation(command: Any) -> tuple[str, str | None] | None:
    """Pull ``(skill, experiment_dir)`` out of a Bash ``emit-skill-return`` command.

    Returns ``None`` unless *command* is a string invoking ``emit-skill-return``
    with a parseable ``--skill`` value. ``experiment_dir`` is the
    ``--experiment-dir`` value when the command carries one (quoted or bare),
    else ``None`` — the caller falls back to the payload's ``cwd``. The skill
    name is *not* checked against :data:`_KNOWN_SKILLS` here; that policy
    check stays in :func:`build_hook_output`.
    """
    if not isinstance(command, str) or _EMIT_INVOCATION_RE.search(command) is None:
        return None
    skill_match = _SKILL_FLAG_RE.search(command)
    if skill_match is None:
        return None
    dir_match = _EXPERIMENT_DIR_FLAG_RE.search(command)
    experiment_dir = None
    if dir_match is not None:
        experiment_dir = next((g for g in dir_match.groups() if g), None)
    return skill_match.group(1), experiment_dir


def _emit_reported_failure(tool_response: Any) -> bool:
    """True when the Bash ``tool_response`` carries a POSITIVE failure signal.

    A failed / interrupted emit commits no fresh envelope (finding #56), so the
    autofetch must not inject a stale one. This inspects the just-run Bash
    call's result for an explicit failure: a non-zero exit code, an interrupted
    flag, or an ``ok: false`` stdout envelope (the shape the emitter prints on a
    validation refusal). Absent any such signal — including an empty or
    non-mapping ``tool_response`` — it returns ``False`` so the hook keeps its
    pre-existing fail-open, additive posture.
    """
    if not isinstance(tool_response, dict):
        return False
    for key in ("exit_code", "exitCode", "returncode", "returnCode", "code"):
        val = tool_response.get(key)
        if isinstance(val, bool):  # a bool is not an exit code
            continue
        if isinstance(val, int) and val != 0:
            return True
    if tool_response.get("interrupted") is True:
        return True
    stdout = tool_response.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        try:
            parsed = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            return True
    return False


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a PostToolUse *payload* to the hook-output dict, or ``None``.

    Returns ``None`` (→ caller prints nothing, a clean no-op) for every case
    that is not "a known sub-skill's ``emit-skill-return`` Bash call just ran
    and its committed envelope is readable":

    * *payload* is not a mapping.
    * the just-completed tool is not ``Bash``.
    * the command does not invoke ``emit-skill-return`` with a resolvable
      ``--skill``, or the skill is not in :data:`_KNOWN_SKILLS`.
    * the committed return file is missing (e.g. the emit itself failed
      validation) or not valid JSON.

    On the happy path it returns the Claude Code PostToolUse hook-output shape::

        {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                "additionalContext": "<envelope JSON>"}}

    where ``additionalContext`` is the verbatim envelope JSON the sub-skill
    committed — the same bytes ``fetch-skill-return`` would print to stdout.
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("tool_name") != "Bash":
        return None

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    invocation = extract_emit_invocation(command)
    if invocation is None:
        return None
    skill, flag_dir = invocation
    if skill not in _KNOWN_SKILLS:
        return None

    # A FAILED / interrupted emit commits no fresh envelope; injecting a stale
    # one left by a prior session would feed the parent skill wrong paths and
    # verdicts (finding #56). Gate on a POSITIVE failure signal from the Bash
    # call that just ran. Fail-open by design: an empty/absent tool_response
    # carries no signal and preserves the hook's pre-existing additive posture.
    if _emit_reported_failure(payload.get("tool_response")):
        return None

    # Prefer the emit command's own --experiment-dir (the authoritative target
    # the emitter wrote to); fall back to the harness cwd, then process cwd.
    cwd = payload.get("cwd")
    if flag_dir:
        experiment_dir = Path(flag_dir)
    elif isinstance(cwd, str) and cwd:
        experiment_dir = Path(cwd)
    else:
        experiment_dir = Path(os.getcwd())

    # The ONE shared reader (also used by the Stop-guard completer): a missing /
    # permission-denied file (the emit itself failed validation) or non-JSON
    # content yields None → the parent's own fetch-skill-return surfaces the
    # typed "skill_return_missing" envelope; the hook stays silent rather than
    # inventing one. The canonical serialization matches fetch-skill-return's
    # stdout exactly.
    additional_context = read_committed_envelope(experiment_dir, skill)
    if additional_context is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        }
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, maybe print, never crash.

    Reads the PostToolUse payload from stdin, runs :func:`build_hook_output`,
    and prints the resulting JSON to stdout when non-``None``. Any unexpected
    error is swallowed and reported as a clean no-op (exit ``0``): a hook must
    never block the tool that just ran. ``argv`` is accepted for symmetry with
    other entrypoints but is unused.
    """
    del argv
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else None
    except (json.JSONDecodeError, ValueError):
        return 0

    try:
        output = build_hook_output(payload)
    except Exception:
        # Last-resort fail-open: a bug in the core must not crash the harness
        # or block the tool call. A silent no-op degrades to today's behaviour
        # (the parent's manual fetch-skill-return still runs).
        return 0

    if output is not None:
        print(json.dumps(output), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the harness
    raise SystemExit(main())
