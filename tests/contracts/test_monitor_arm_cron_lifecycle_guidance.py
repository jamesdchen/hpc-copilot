"""Contract: the skills that relay ``monitor_arm`` document the cron DELETE too.

``decide-monitor-arm`` returns ``arm == "none"`` at terminal precisely so the
next tick tears the cron down, and ``docs/primitives/decide-monitor-arm.md``
mandates "the slash command must CronDelete any prior cron for the run_id when
arm is none" — but until run #8 no agent-facing surface carried the rule, so
the demo agent improvised the CronCreate from ``cron_create_args`` and nothing
ever instructed the delete: a ``*/1`` headless monitor kept firing against a
finished, then WIPED, run. Create improvised, delete never.

This binds the full lifecycle (create-verbatim, delete-at-none, one cron per
run) to every surface that relays a ``monitor_arm`` brief, so the primitive's
mandate and the agent guidance can never drift apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SURFACES = (
    _REPO_ROOT / "src/hpc_agent/slash_commands/skills/hpc-status/SKILL.md",
    _REPO_ROOT / "src/hpc_agent/slash_commands/skills/hpc-submit/SKILL.md",
)
_COMMAND = _REPO_ROOT / "src/hpc_agent/slash_commands/commands/monitor-hpc.md"

# The distinctive phrase carrying the rule — kept in lockstep with the SKILLs.
_MARKER = "cron lifecycle"


def _block_with(text: str, needle: str) -> str:
    """Return the rule's whole block: from the line containing *needle* to the
    next ``##`` heading (or end of file). Section-aware, not paragraph-aware —
    in hpc-status the rule is a heading + bullet list, in hpc-submit a single
    indented bullet."""
    low = text.lower()
    start = low.find(needle)
    if start < 0:
        return ""
    end = text.find("\n## ", start)
    return text[start:] if end < 0 else text[start:end]


def test_monitor_arm_surfaces_document_the_full_cron_lifecycle() -> None:
    for surface in _SURFACES:
        text = surface.read_text(encoding="utf-8")
        block = _block_with(text, _MARKER)
        assert block, (
            f"{surface.name} ({surface.parent.name}) must carry the monitor-arm "
            f"cron lifecycle rule (marker {_MARKER!r}) — the delete half of the "
            f"docs/primitives/decide-monitor-arm.md mandate"
        )
        assert "CronCreate" in block and "cron_create_args" in block, (
            f"{surface.parent.name}: the rule must bind arm=='cron' to passing "
            f"cron_create_args to CronCreate verbatim (code owns the schedule)"
        )
        assert "CronDelete" in block, (
            f"{surface.parent.name}: the rule must bind arm=='none' (terminal) "
            f"to CronDelete — creating without ever deleting is the run #8 "
            f"stale-monitor bug"
        )
        assert re.search(r"VERBATIM", block), (
            f"{surface.parent.name}: cron_create_args must be passed VERBATIM — "
            f"a hand-composed schedule is the improvisation class this kills"
        )


def test_status_skill_deletes_the_cron_for_an_unresolvable_run() -> None:
    """A tick firing against a wiped/unknown run must delete itself — else a
    journal wipe leaves the cron polling a ghost forever."""
    text = _SURFACES[0].read_text(encoding="utf-8")
    block = _block_with(text, _MARKER)
    assert re.search(r"cannot resolve|unknown|wiped", block), (
        "hpc-status: the lifecycle rule must cover the unresolvable-run_id tick "
        "(journal wiped) — treat as arm=='none' and delete the firing cron"
    )


def test_monitor_command_mirrors_the_delete_mandate() -> None:
    text = _COMMAND.read_text(encoding="utf-8")
    assert "CronDelete" in text, (
        "monitor-hpc.md must mirror the delete half of the cron lifecycle "
        "(the primitive doc's mandate is dead prose if no command carries it)"
    )
