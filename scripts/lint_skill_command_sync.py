#!/usr/bin/env python3
"""Cross-check ``skills/`` against ``src/slash_commands/commands/``.

Both trees describe the same workflows (submit, monitor, aggregate,
campaign, preflight, build-executor) in different prose. This lint
catches the most common drift modes:

1. A skill exists with no matching slash command (or vice versa).
2. A skill or slash-command file is missing required frontmatter.
3. A skill's declared ``execution`` mode disagrees with how its
   command routes (``delegated`` ⇔ an ``hpc_spawn`` Task request or an
   ``hpc-agent run`` Bash call).

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
    ("hpc-classify-axis", "classify-axis-hpc"),
]

# Slash-command files allowed to have no skill counterpart (e.g.
# scaffolding commands that don't expose a long-form skill surface).
SLASH_ONLY_OK: set[str] = {"validate-campaign"}


# A workflow command must explicitly route to its skill rather than run
# the workflow from the slash body alone. Accepted routing forms: the
# inline "Invoke the `<skill>` skill" directive; the subagent-execute
# form ("... subagent ... to execute it (`skills/<id>/SKILL.md`)"); or
# the thin-trigger form — shelling `hpc-agent run <workflow>`, the
# code-orchestrated entrypoint.
_INVOKE_DIRECTIVE_RE = re.compile(
    r"[Ii]nvoke the [`*]?[a-z][a-z0-9-]+[`*]? skill"
    r"|subagent[^\n]*?to execute it \(`skills/[a-z0-9-]+/SKILL\.md`\)"
    r"|hpc-agent run "
)

# Every workflow skill statically declares, in its frontmatter, whether
# it runs `delegated` (in a fresh-context subagent, spawned from a
# content-addressed spec) or `inline` (in the main conversation). This
# is an authored property, not a per-invocation judgement — the lint
# below cross-checks it against how the paired command routes.
_EXECUTION_RE = re.compile(r"^execution:\s*(delegated|inline)\s*$", re.MULTILINE)


def main() -> int:
    errors: list[str] = []

    skill_ids_present = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
    slash_ids_present = {p.stem for p in COMMANDS_DIR.glob("*.md")}

    declared_skills = {pair[0] for pair in WORKFLOW_PAIRS}
    declared_slashes = {pair[1] for pair in WORKFLOW_PAIRS}

    # Each declared pair must have both files. The slash body must also
    # route to its skill — either an inline "Invoke the `<skill>` skill"
    # directive or the subagent-delegation form — otherwise the slash
    # collapsed away its own workflow-mechanics content and the agent has
    # nothing to work from. The regex tolerates `name`/**name**/plain
    # wrapping.
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
                "imperative skill-routing directive (regex "
                f"{_INVOKE_DIRECTIVE_RE.pattern!r}). A workflow command must "
                "route to its skill — invoke it, delegate it to a subagent, "
                "or shell `hpc-agent run` — without the directive the agent "
                "may try to do the workflow from the slash body alone, which "
                "lacks the workflow mechanics by design."
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

        # The skill's declared `execution` mode must agree with how its
        # command routes: `delegated` ⇒ the command delegates via an
        # `hpc_spawn` Task request or an `hpc-agent run` Bash call;
        # `inline` ⇒ it does neither.
        if not skill_path.is_file():
            continue
        skill_body = skill_path.read_text(encoding="utf-8")
        exec_match = _EXECUTION_RE.search(skill_body)
        if exec_match is None:
            errors.append(
                f"{skill_path.relative_to(REPO_ROOT)} is missing a frontmatter "
                "`execution: delegated|inline` field. Every workflow skill "
                "must statically declare how it runs."
            )
            continue
        routes_via_spawn = "hpc_spawn" in body or "hpc-agent run" in body
        if exec_match.group(1) == "delegated" and not routes_via_spawn:
            errors.append(
                f"{skill_id!r} declares `execution: delegated` but its command "
                f"{slash_path.relative_to(REPO_ROOT)} does not delegate via an "
                "`hpc_spawn` Task request or an `hpc-agent run` Bash call. A "
                "delegated skill must run in a fresh-context worker."
            )
        if exec_match.group(1) == "inline" and routes_via_spawn:
            errors.append(
                f"{skill_id!r} declares `execution: inline` but its command "
                f"{slash_path.relative_to(REPO_ROOT)} routes through an "
                "`hpc_spawn` Task request or an `hpc-agent run` Bash call. An "
                "inline skill runs in the main conversation — drop the spawn "
                "routing, or mark the skill `delegated`."
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
