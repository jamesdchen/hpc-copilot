"""``Stop`` hook — block ending the turn over an unfetched sub-skill return.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.Stop`` array
(see :func:`hpc_agent.agent_assets.install_agent_assets`). It is invoked when
the agent is about to end its turn, receives the Stop payload as JSON on
**stdin**, and may emit ``{"decision": "block", "reason": ...}`` on **stdout**
to make the agent continue instead.

Why it exists
-------------
The sub-skill `Then stop`/hand-back prose at every composition boundary is
advisory: a parent composing ``Skill(<sub>)`` can still end its turn right
after the sub-skill's ``emit-skill-return``, leaving the committed envelope
unfetched and the parent procedure stalled until a human types "keep going"
(empirical: 2026-06-10 demo, ``hpc-wrap-entry-point``). Prose lowers the
frequency; nothing harness-side *prevented* the stop — ``PostToolUse`` hooks
only run when a tool call happens, and the failure mode is precisely that no
further tool call happens. ``Stop`` is the one hook event that fires at the
exact failure point, so this guard turns "stopped over a pending return" into
a deterministic continuation.

Behaviour
---------
On a Stop event, the guard scans ``<cwd>/.hpc/_returns/`` for a committed
envelope of any skill in :data:`hpc_agent.cli.skill_returns._KNOWN_SKILLS`.
If one or more exist, it blocks the stop with a reason instructing the agent
to ``fetch-skill-return`` each pending skill and continue the parent skill's
next step.

The condition is **self-healing**: ``fetch-skill-return`` deletes the
committed file by default, so after the agent follows the reason the guard has
nothing left to block on — including for a stale envelope left over by an
older session, which gets flushed by exactly one block-fetch-continue cycle.

Loop safety & defensiveness
---------------------------
* If the payload carries ``stop_hook_active`` (Claude Code's marker that this
  stop is already a continuation forced by a Stop hook), the guard is a
  no-op — it can block a given stop at most once, never loop.
* For a malformed payload, an unreadable directory, or no pending envelope it
  is a **clean no-op** — prints nothing, exits ``0``. It never raises and
  never exits non-zero.
* In a directory with no ``.hpc/_returns/`` (any non-hpc project — the hook is
  installed user-globally) the scan is a handful of ``is_file`` misses.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

# Single source of truth for the set of sub-skills that emit a return envelope.
# Re-exported from the CLI primitive so this hook and the verbs can never drift.
from hpc_agent.cli.skill_returns import _KNOWN_SKILLS, _committed_path

__all__ = ["build_hook_output", "main", "pending_skill_returns"]


def pending_skill_returns(experiment_dir: Path) -> list[str]:
    """Known skills with a committed (not merely staged) envelope on disk.

    Order follows :data:`_KNOWN_SKILLS`. Filesystem errors on any single probe
    are swallowed — a skill we cannot stat is treated as not pending.
    """
    pending: list[str] = []
    for skill in _KNOWN_SKILLS:
        try:
            if _committed_path(experiment_dir, skill).is_file():
                pending.append(skill)
        except OSError:
            continue
    return pending


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a Stop *payload* to a block decision, or ``None``.

    Returns ``None`` (→ caller prints nothing, the stop proceeds) when:

    * *payload* is not a mapping.
    * ``stop_hook_active`` is truthy — this stop is already a hook-forced
      continuation; blocking again would loop.
    * no known skill has a committed envelope under the resolved
      experiment dir.

    Otherwise returns the Claude Code Stop hook-output shape::

        {"decision": "block", "reason": "<fetch instructions>"}
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("stop_hook_active"):
        return None

    cwd = payload.get("cwd")
    experiment_dir = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())

    pending = pending_skill_returns(experiment_dir)
    if not pending:
        return None

    # Forward-slash form: the agent will paste this into a Git Bash command,
    # where a bare backslash path invites the \U-escape-collapse bug class
    # (agent_assets._hook_python). shlex.quote still covers spaces.
    quoted_dir = shlex.quote(experiment_dir.as_posix())
    fetches = " && ".join(
        f"hpc-agent fetch-skill-return --skill {skill} --experiment-dir {quoted_dir}"
        for skill in pending
    )
    reason = (
        f"Sub-skill return envelope(s) committed but not fetched: "
        f"{', '.join(pending)}. Run `{fetches}`, then continue the parent "
        "skill's next step — a sub-skill composition boundary is not the end "
        "of the turn."
    )
    return {"decision": "block", "reason": reason}


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, maybe print, never crash.

    Reads the Stop payload from stdin, runs :func:`build_hook_output`, and
    prints the resulting JSON to stdout when non-``None``. Any unexpected
    error is swallowed and reported as a clean no-op (exit ``0``): a broken
    guard must degrade to today's behaviour (the stop proceeds, the manual
    "keep going" seam remains), never wedge the harness. ``argv`` is accepted
    for symmetry with other entrypoints but is unused.
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
