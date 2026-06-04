"""``PostToolUse`` hook — auto-fetch a sub-skill's return envelope.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.PostToolUse``
array (see :func:`hpc_agent.agent_assets.install_agent_assets`). It is invoked
by the harness after every tool call, receives the PostToolUse payload as JSON
on **stdin**, and may emit a JSON object on **stdout** to inject context into
the agent's next observation.

Why it exists
-------------
A parent skill that composes ``Skill(<sub>)`` must, by prose discipline, chain
``hpc-agent fetch-skill-return --skill <sub>`` to read the sub-skill's return
envelope from ``<experiment_dir>/.hpc/_returns/<skill>.json`` (see
:mod:`hpc_agent.cli.skill_returns`). That manual follow-up is one of two seams
where the parent's prose-discipline still matters. This hook removes it: after a
composed ``Skill(<sub>)`` for a *known* sub-skill returns, it reads the committed
return envelope and injects it as ``additionalContext`` so the envelope is in the
agent's next observation whether or not the parent remembered to fetch it.

Contract & defensiveness
------------------------
The hook is a **pure, additive, fail-open** observer:

* It only acts when the just-completed tool is ``Skill`` and the resolved
  sub-skill name is in :data:`hpc_agent.cli.skill_returns._KNOWN_SKILLS`.
* For any other tool, an unknown/unresolvable skill, a missing or malformed
  return file, or a malformed payload, it is a **clean no-op** — it prints
  nothing and exits ``0``. It never raises and never exits non-zero, so it can
  never block a tool call or crash the harness.
* It does **not** delete the return file (unlike the manual
  ``fetch-skill-return``, which clears by default). Leaving the file on disk
  keeps the existing parent-side ``fetch-skill-return`` prose working
  unchanged — the injection is purely additive context, not a replacement for
  the documented seam.

The ``experiment_dir`` is taken from the payload's ``cwd`` (the directory the
harness ran the tool in), which is the experiment directory skills operate
from. If ``cwd`` is absent we fall back to :func:`os.getcwd`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Single source of truth for the set of sub-skills that emit a return envelope.
# Re-exported from the CLI primitive so this hook and the verbs can never drift.
from hpc_agent.cli.skill_returns import _KNOWN_SKILLS, _committed_path

__all__ = ["build_hook_output", "extract_skill_name", "main"]


def extract_skill_name(tool_input: Any) -> str | None:
    """Resolve the invoked sub-skill name from a ``Skill`` tool's ``tool_input``.

    Claude Code's ``Skill`` tool carries the target skill in its input. The
    field name has historically been ``command`` (the slash/skill name) but a
    few payload shapes use ``skill`` or ``name``; we accept any of them so a
    harness field rename doesn't silently disable the hook. Returns the trimmed
    string, or ``None`` if *tool_input* is not a mapping or carries no
    recognisable skill field.
    """
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "skill", "name"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a PostToolUse *payload* to the hook-output dict, or ``None``.

    Returns ``None`` (→ caller prints nothing, a clean no-op) for every case
    that is not "a known sub-skill's ``Skill`` call just returned and its
    committed envelope is readable":

    * *payload* is not a mapping.
    * the just-completed tool is not ``Skill``.
    * the resolved skill name is absent or not in :data:`_KNOWN_SKILLS`.
    * the committed return file is missing or not valid JSON.

    On the happy path it returns the Claude Code PostToolUse hook-output shape::

        {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                "additionalContext": "<envelope JSON>"}}

    where ``additionalContext`` is the verbatim envelope JSON the sub-skill
    committed — the same bytes ``fetch-skill-return`` would print to stdout.
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("tool_name") != "Skill":
        return None

    skill = extract_skill_name(payload.get("tool_input"))
    if skill is None or skill not in _KNOWN_SKILLS:
        return None

    # ``cwd`` is the directory the harness ran the tool in — the experiment
    # directory skills operate from. Fall back to the process cwd if absent.
    cwd = payload.get("cwd")
    experiment_dir = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())

    committed = _committed_path(experiment_dir, skill)
    try:
        envelope_text = committed.read_text(encoding="utf-8")
    except OSError:
        # Missing file / permission / not-a-file → the parent's own
        # fetch-skill-return surfaces the typed "skill_return_missing"
        # envelope. The hook stays silent rather than inventing one.
        return None

    try:
        # Parse only to validate it is JSON; re-serialise canonically so the
        # injected context matches fetch-skill-return's stdout exactly.
        envelope = json.loads(envelope_text)
    except (json.JSONDecodeError, ValueError):
        return None

    additional_context = json.dumps(envelope, sort_keys=True)
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
