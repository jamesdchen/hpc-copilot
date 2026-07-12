"""ONE definition of the agent-facing prose lints' scan roots.

Both agent-facing prose lints — ``lint_no_raw_ssh`` and
``lint_no_blocklisted_commands`` — scan the same surface. Before this
module each hard-coded its own ``_SKILL_GLOB`` / ``_WORKER_PROMPT_GLOB``
pair, and ``.pre-commit-config.yaml`` duplicated the same paths a third
time in each hook's ``files:`` trigger. Three hand-maintained copies of
one scan root is the G10/B11 drift class (a scan root that silently
covers 3 of N surfaces). This module is the single Python source; the
pre-commit ``files:`` filters remain a hand-written trigger only (YAML
can't import Python) but are functionally redundant — every lint runs
``pass_filenames: false`` and re-derives its own targets from here.

**Retired: the ``_kernel/extension/worker_prompts/*.md`` glob.** Under the
three-layer architecture the worker prompt is *rendered* by
``spawn_prompt`` from ``category: worker-prompt`` skills — no standalone
``.md`` ships under ``worker_prompts/`` (the directory is empty), so the
glob matched nothing on the real tree. Retiring it stops the default
scan from walking a dead path. The worker-strictness lint LOGIC
(invoke-only chaining in ``lint_no_blocklisted_commands``) is retained
and still exercised by synthetic fixtures via ``lint_file`` directly —
only the dead real-tree glob is gone.
"""

from __future__ import annotations

from pathlib import Path

# Scan-root-relative glob for the agent-facing prose surface. A skill body
# (``SKILL.md``) is the one agent-facing prose surface that ships as an
# authored ``.md`` file today.
SKILL_GLOB = "hpc_agent/slash_commands/skills/*/SKILL.md"


def iter_agent_prose_targets(scan_root: Path) -> list[Path]:
    """Every agent-facing prose file under *scan_root*, sorted.

    *scan_root* is a ``src``-shaped directory (the real one or a test
    fixture root). Returns only existing files.
    """
    return [p for p in sorted(scan_root.glob(SKILL_GLOB)) if p.is_file()]
