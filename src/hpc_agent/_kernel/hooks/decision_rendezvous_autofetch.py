"""``PostToolUse`` hook — inject the brief a ``block-drive`` tick just parked.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.PostToolUse``
array (see :func:`hpc_agent.agent_assets.install_agent_assets`). It is invoked
by the harness after a matched tool call, receives the PostToolUse payload as
JSON on **stdin**, and may emit a JSON object on **stdout** to inject context
into the agent's next observation.

Why it exists
-------------
This generalizes the ``skill-return`` autofetch hook to the ``block-drive``
decision rendezvous (``docs/design/block-drive.md`` §5 Phase-1 note: "a
PostToolUse hook, mirroring skill-return autofetch, can inject the brief"). When
a ``block-drive`` tick chains a deterministic span and hits a block's decision,
it writes a ``pending_decision`` marker carrying the ``brief`` and exits. The
LLM must render that brief as a proposal — but the brief only reaches the LLM as
the verb's stdout, which the agent can miss. The moment the ``block-drive`` Bash
call returns, this hook reads the freshly-parked marker back and injects the
brief as ``additionalContext`` so the LLM reliably has it to render.

Contract & defensiveness
------------------------
The hook is a **pure, additive, fail-open** observer:

* It only acts when the just-completed tool is ``Bash``, its command invokes
  ``block-drive`` with a resolvable ``--run-id``, and that run currently carries
  a non-empty ``pending_decision`` marker with a ``brief``.
* For any other tool, an unparseable command, a run that is not parked (the tick
  advanced rather than parking, or the run does not exist), or a malformed
  payload, it is a **clean no-op** — it prints nothing and exits ``0``. It never
  raises and never exits non-zero, so it can never block a tool call or crash
  the harness.
* It is purely additive — it never clears the marker (the driver's next tick
  owns that) and never deletes anything.

The ``experiment_dir`` is taken from the command's own ``--experiment-dir`` flag
when present, falling back to the payload's ``cwd`` and finally
:func:`os.getcwd`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

__all__ = ["build_hook_output", "extract_drive_invocation", "main"]

# ``--run-id <id>`` / ``--run-id=<id>`` (optionally quoted) inside the Bash
# command. Run-id charset mirrors the RunId slug (letters, digits, ._-).
_RUN_ID_FLAG_RE = re.compile(r"--run-id(?:=|\s+)['\"]?([A-Za-z0-9][A-Za-z0-9._-]*)")
# ``--experiment-dir <path>`` — double-quoted, single-quoted, or a bare token
# (stopping at whitespace and shell metacharacters so a chained ``&& next``
# or a closing paren is not swallowed into the path). Mirrors the autofetch
# sibling exactly.
_EXPERIMENT_DIR_FLAG_RE = re.compile(
    r"--experiment-dir(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|)]+))"
)


def extract_drive_invocation(command: Any) -> tuple[str, str | None] | None:
    """Pull ``(run_id, experiment_dir)`` out of a Bash ``block-drive`` command.

    Returns ``None`` unless *command* is a string invoking ``block-drive`` with
    a parseable ``--run-id`` value. ``experiment_dir`` is the
    ``--experiment-dir`` value when the command carries one (quoted or bare),
    else ``None`` — the caller falls back to the payload's ``cwd``.
    """
    if not isinstance(command, str) or "block-drive" not in command:
        return None
    run_match = _RUN_ID_FLAG_RE.search(command)
    if run_match is None:
        return None
    dir_match = _EXPERIMENT_DIR_FLAG_RE.search(command)
    experiment_dir = None
    if dir_match is not None:
        experiment_dir = next((g for g in dir_match.groups() if g), None)
    return run_match.group(1), experiment_dir


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a PostToolUse *payload* to the hook-output dict, or ``None``.

    Returns ``None`` (→ caller prints nothing, a clean no-op) for every case
    that is not "a ``block-drive`` Bash call just ran and left a parked run with
    a brief":

    * *payload* is not a mapping.
    * the just-completed tool is not ``Bash``.
    * the command does not invoke ``block-drive`` with a resolvable
      ``--run-id``.
    * the run is not parked on a decision, or its marker carries no ``brief``.

    On the happy path it returns the Claude Code PostToolUse hook-output shape::

        {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                "additionalContext": "<brief JSON>"}}

    where ``additionalContext`` is the ``brief`` the ``block-drive`` tick just
    parked, canonically serialized.
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("tool_name") != "Bash":
        return None

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    invocation = extract_drive_invocation(command)
    if invocation is None:
        return None
    run_id, flag_dir = invocation

    cwd = payload.get("cwd")
    if flag_dir:
        experiment_dir = Path(flag_dir)
    elif isinstance(cwd, str) and cwd:
        experiment_dir = Path(cwd)
    else:
        experiment_dir = Path(os.getcwd())

    from hpc_agent.state.journal import read_pending_decision

    try:
        marker = read_pending_decision(run_id, experiment_dir=experiment_dir)
    except OSError:
        return None
    if not isinstance(marker, dict):
        return None
    brief = marker.get("brief")
    if not brief:
        # Not parked, or parked with no brief — nothing to inject. The tick may
        # have advanced (consumed a prior decision) rather than parking.
        return None

    additional_context = json.dumps(brief, sort_keys=True, default=str)
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
        return 0

    if output is not None:
        print(json.dumps(output), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the harness
    raise SystemExit(main())
