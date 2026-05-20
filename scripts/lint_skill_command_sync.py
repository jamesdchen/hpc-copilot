#!/usr/bin/env python3
"""Cross-check ``skills/`` against ``src/slash_commands/commands/``.

Both trees describe the same workflows (submit, monitor, aggregate,
campaign, preflight, build-executor) in different prose. This lint
catches the most common drift modes:

1. A skill exists with no matching slash command (or vice versa).
2. A skill or slash-command file is missing required frontmatter.

It deliberately does **not** diff the bodies — the two surfaces have
different audiences (agent skill vs. interactive slash-command prompt)
and are expected to differ in tone. The contract is just that the *set*
of workflows stays in sync.

Mapping rules
-------------

* ``src/slash_commands/skills/<id>/SKILL.md``  ↔  ``src/slash_commands/commands/<cmd>.md``
* The ``<id>`` and ``<cmd>`` may differ (e.g. ``hpc-submit`` vs
  ``submit-hpc``). The mapping below pins which pair represents the
  same workflow.

Add a new pair to ``WORKFLOW_PAIRS`` when introducing a new workflow.

Usage::

    uv run python scripts/lint_skill_command_sync.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "src" / "slash_commands" / "skills"
COMMANDS_DIR = REPO_ROOT / "src" / "slash_commands" / "commands"

# Each tuple: (skill_id, slash_command_stem). Both files must exist.
WORKFLOW_PAIRS: list[tuple[str, str]] = [
    ("hpc-submit", "submit-hpc"),
    ("hpc-status", "monitor-hpc"),
    ("hpc-aggregate", "aggregate-hpc"),
    ("hpc-campaign", "campaign-hpc"),
    ("hpc-preflight", "preflight"),
    ("hpc-build-executor", "hpc-axes-init"),
]

# Slash-command files allowed to have no skill counterpart (e.g.
# scaffolding commands that don't expose a long-form skill surface).
SLASH_ONLY_OK: set[str] = {"validate-campaign"}


_INVOKE_DIRECTIVE_RE = re.compile(r"[Ii]nvoke the [`*]?[a-z][a-z0-9-]+[`*]? skill")


def main() -> int:
    errors: list[str] = []

    skill_ids_present = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
    slash_ids_present = {p.stem for p in COMMANDS_DIR.glob("*.md")}

    declared_skills = {pair[0] for pair in WORKFLOW_PAIRS}
    declared_slashes = {pair[1] for pair in WORKFLOW_PAIRS}

    # Each declared pair must have both files. The slash body must also
    # contain an explicit "Invoke the `<skill>` skill" directive — without
    # it, the slash collapsed away its own workflow-mechanics content
    # under the surgical-split refactor and the agent has nothing to
    # work from. The regex tolerates `name`/**name**/plain wrapping.
    for skill_id, slash_stem in WORKFLOW_PAIRS:
        skill_path = SKILLS_DIR / skill_id / "SKILL.md"
        slash_path = COMMANDS_DIR / f"{slash_stem}.md"
        if not skill_path.is_file():
            errors.append(
                f"declared workflow pair ({skill_id!r}, {slash_stem!r}) but "
                f"{skill_path.relative_to(REPO_ROOT)} is missing"
            )
        if not slash_path.is_file():
            errors.append(
                f"declared workflow pair ({skill_id!r}, {slash_stem!r}) but "
                f"{slash_path.relative_to(REPO_ROOT)} is missing"
            )
            continue
        body = slash_path.read_text(encoding="utf-8")
        if not _INVOKE_DIRECTIVE_RE.search(body):
            errors.append(
                f"{slash_path.relative_to(REPO_ROOT)} is missing the "
                "imperative skill-invocation directive (regex "
                f"{_INVOKE_DIRECTIVE_RE.pattern!r}). Slash commands must "
                "explicitly tell the agent to invoke the matching skill via "
                "the Skill tool — without the directive, the agent may try "
                "to do the workflow from the slash body alone, which lacks "
                "the workflow mechanics by design."
            )
            continue
        # Stronger check: the directive should name *this pair's* skill_id.
        if skill_id not in body:
            errors.append(
                f"{slash_path.relative_to(REPO_ROOT)} contains an invocation "
                f"directive but does not name the matching skill {skill_id!r}. "
                "Either fix the slash body to invoke the right skill, or "
                "update WORKFLOW_PAIRS in this lint script."
            )

    # Skills present on disk but not declared in the pair table.
    undeclared_skills = skill_ids_present - declared_skills
    if undeclared_skills:
        errors.append(
            "skill(s) on disk with no entry in WORKFLOW_PAIRS: "
            f"{sorted(undeclared_skills)}. Add them to "
            "scripts/lint_skill_command_sync.py:WORKFLOW_PAIRS so the "
            "two surfaces stay in sync."
        )

    # Slash commands without a declared skill (and not allow-listed).
    undeclared_slashes = slash_ids_present - declared_slashes - SLASH_ONLY_OK
    if undeclared_slashes:
        errors.append(
            "slash command(s) on disk with no entry in WORKFLOW_PAIRS "
            f"and not in SLASH_ONLY_OK: {sorted(undeclared_slashes)}"
        )

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(f"skills <-> slash_commands in sync ({len(WORKFLOW_PAIRS)} workflow pairs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
