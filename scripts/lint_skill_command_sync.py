#!/usr/bin/env python3
"""Cross-check ``skills/`` against ``src/slash_commands/commands/``.

Both trees describe the same workflows (submit, monitor, aggregate,
campaign, build-executor, classify-axis) in different prose: the skill
is the agent-autonomous decision logic (callable by any agent — the
user's chat via the Skill tool, or another harness like a MARs
experiment agent via direct read); the slash is the human-elicitation
wrapper that gathers intent and invokes the skill with a fully-resolved
spec. See ``docs/internals/skill-policy.md``. Environment-authority
work (the former ``hpc-preflight`` skill) moved to ``hpc-agent setup``.
This lint catches the most common drift modes:

1. A skill exists with no matching slash command (or vice versa).
2. A skill or slash-command file is missing required frontmatter.
3. A skill's declared ``execution`` mode disagrees with how its
   command routes (``delegated`` ⇔ an ``hpc_spawn`` Task request or an
   ``hpc-agent run`` Bash call).
4. A skill's declared ``category`` (the policy witness) disagrees
   with its ``execution`` mode — see ``docs/internals/skill-policy.md``.

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
#
# Empty by design after the slash-condensation pass: the user-facing
# slash surface is now exactly the four workflow triggers
# (WORKFLOW_TRIGGER_SLASHES). The three skills (hpc-build-executor,
# hpc-classify-axis, hpc-wrap-entry-point) are agent-only — callable
# via the Skill tool by the in-chat agent (when /submit-hpc escalates)
# or by another agent harness like MARs directly. Skills without a
# paired slash are allow-listed in SKILL_ONLY_OK below.
WORKFLOW_PAIRS: list[tuple[str, str]] = []

# Slash commands that route through `hpc-agent run <workflow>` to the
# spawn pipeline rather than to a paired Skill. Their workflow lives in
# ``src/hpc_agent/worker_prompts/<workflow>.md`` (see
# ``docs/internals/skill-policy.md``), not in any ``SKILL.md``. The
# routing lint below skips the skill-pair check for these.
WORKFLOW_TRIGGER_SLASHES: set[str] = {
    "submit-hpc",
    "monitor-hpc",
    "aggregate-hpc",
    "campaign-hpc",
}

# Skills that ship without a paired slash command. Under the
# agent-autonomous policy (docs/internals/skill-policy.md) every skill
# falls into this bucket — the in-chat agent invokes them via the Skill
# tool when a workflow escalates, and other agent harnesses read them
# directly. No slash is needed because the human-elicitation prose
# lives in the workflow-trigger slash that escalates.
SKILL_ONLY_OK: set[str] = {
    "hpc-build-executor",
    "hpc-classify-axis",
    "hpc-wrap-entry-point",
}

# Slash-command files allowed to have no skill counterpart. Empty after
# the condensation pass — every remaining slash is either a workflow
# trigger (above) or has been removed.
SLASH_ONLY_OK: set[str] = set()


# A workflow command must explicitly route to its skill rather than run
# the workflow from the slash body alone. Accepted routing forms: the
# inline "Invoke the `<skill>` skill" directive; the subagent-execute
# form ("... subagent ... to execute it (`skills/<id>/SKILL.md`)"); the
# thin-trigger form — shelling `hpc-agent run <workflow>`, the
# code-orchestrated entrypoint; or, for the campaign loop, shelling
# `hpc-campaign-driver`, the code-orchestrated tick-by-tick driver.
_INVOKE_DIRECTIVE_RE = re.compile(
    # ``[\`*]{0,2}`` (not ``[\`*]?``) so the directive accepts
    # ``**hpc-submit**`` and ``\`hpc-submit\``` wrapping — the comment
    # above promises bold wrapping is tolerated.
    r"[Ii]nvoke the [`*]{0,2}[a-z][a-z0-9-]+[`*]{0,2} skill"
    r"|subagent[^\n]*?to execute it \(`skills/[a-z0-9-]+/SKILL\.md`\)"
    r"|hpc-agent run\b"
    r"|hpc-campaign-driver"
)

# Every workflow skill statically declares, in its frontmatter, whether
# it runs `delegated` (in a fresh-context subagent, spawned from a
# content-addressed spec) or `inline` (in the main conversation). This
# is an authored property, not a per-invocation judgement — the lint
# below cross-checks it against how the paired command routes.
_EXECUTION_RE = re.compile(r"^execution:\s*(delegated|inline)\s*$", re.MULTILINE)

# Every workflow skill also declares its policy category — the witness
# for docs/internals/skill-policy.md:
#
#   * ``agent-autonomous``     — runs inline; consumed via the Skill tool
#     (by the user's interactive Claude Code chat) or by direct read (by
#     another agent harness like MARs). The body MUST be deterministic
#     given its inputs — no ``[Y/n]`` prompts — so any agent caller can
#     drive it without a human in the loop. Human elicitation, when
#     needed, lives in the paired slash command and feeds the skill a
#     fully-resolved spec.
#   * ``worker-prompt``        — gets inlined as text into the
#     code-rendered ``claude -p --bare`` worker prompt via
#     ``spawn_prompt._procedure_body``; the deterministic prefix means
#     these are eligible for prose hardening (snapshot tests, token
#     budgets, banned-construct lints) that real skills are not.
#
# The category must agree with ``execution``: ``agent-autonomous``
# ⇔ ``inline``; ``worker-prompt`` ⇔ ``delegated``. This pairing is the
# machine-readable expression of the skill-policy rule.
_CATEGORY_RE = re.compile(r"^category:\s*(agent-autonomous|worker-prompt)\s*$", re.MULTILINE)
_CATEGORY_BY_EXECUTION = {
    "inline": "agent-autonomous",
    "delegated": "worker-prompt",
}


def _check_skill_frontmatter(
    skill_id: str, skill_path: Path, errors: list[str]
) -> str | None:
    """Validate a skill's `execution` and `category` frontmatter. Return
    the resolved `execution` value (`"inline"` / `"delegated"`) on
    success, or `None` if anything was wrong (errors appended)."""
    skill_body = skill_path.read_text(encoding="utf-8")
    exec_match = _EXECUTION_RE.search(skill_body)
    if exec_match is None:
        errors.append(
            f"{skill_path.relative_to(REPO_ROOT)} is missing a frontmatter "
            "`execution: delegated|inline` field. Every workflow skill "
            "must statically declare how it runs."
        )
        return None
    category_match = _CATEGORY_RE.search(skill_body)
    if category_match is None:
        errors.append(
            f"{skill_path.relative_to(REPO_ROOT)} is missing a frontmatter "
            "`category: agent-autonomous|worker-prompt` field. See "
            "docs/internals/skill-policy.md — the category records "
            "whether this skill is consumed via the Skill tool / direct "
            "read by an agent (autonomous), or inlined into a worker "
            "prompt for delegated execution."
        )
        return None
    expected_category = _CATEGORY_BY_EXECUTION[exec_match.group(1)]
    if category_match.group(1) != expected_category:
        errors.append(
            f"{skill_id!r} declares `execution: {exec_match.group(1)}` but "
            f"`category: {category_match.group(1)}` — expected "
            f"`category: {expected_category}`. See "
            "docs/internals/skill-policy.md: inline execution ↔ "
            "agent-autonomous; delegated execution ↔ worker-prompt."
        )
        return None
    return exec_match.group(1)


def main() -> int:
    errors: list[str] = []

    skill_ids_present = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
    slash_ids_present = {p.stem for p in COMMANDS_DIR.glob("*.md")}

    declared_skills = {pair[0] for pair in WORKFLOW_PAIRS}
    declared_slashes = {pair[1] for pair in WORKFLOW_PAIRS}

    # Every skill on disk gets its frontmatter validated (execution +
    # category agreement), independent of whether it's paired with a
    # slash. The pairing checks come after.
    for skill_id in sorted(skill_ids_present):
        skill_path = SKILLS_DIR / skill_id / "SKILL.md"
        _check_skill_frontmatter(skill_id, skill_path, errors)

    # Each declared pair must have both files, and the slash body must
    # route to the skill — either via an "Invoke the `<skill>` skill"
    # directive, the subagent-delegation form, or by shelling
    # `hpc-agent run`/`hpc-campaign-driver`. Empty by default; entries
    # exist only when a skill ships with a paired user-typed slash.
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
        if skill_id not in body:
            errors.append(
                f"{slash_path.relative_to(REPO_ROOT)} contains an invocation "
                f"directive but does not name the matching skill {skill_id!r}. "
                "Either fix the slash body to invoke the right skill, or "
                "update WORKFLOW_PAIRS in this lint script."
            )
        if not skill_path.is_file():
            continue
        execution = _check_skill_frontmatter(skill_id, skill_path, errors)
        if execution is None:
            continue
        # Match `hpc-agent run` followed by whitespace / end / backtick /
        # quote. Plain `\b` matches between `run` and `-` because `-` is
        # non-word, falsely accepting `hpc-agent run-time` etc.
        routes_via_spawn = (
            "hpc_spawn" in body
            or re.search(r"hpc-agent run(?![\w-])", body) is not None
            or "hpc-campaign-driver" in body
        )
        if execution == "delegated" and not routes_via_spawn:
            errors.append(
                f"{skill_id!r} declares `execution: delegated` but its command "
                f"{slash_path.relative_to(REPO_ROOT)} does not delegate via an "
                "`hpc_spawn` Task request or an `hpc-agent run` Bash call. A "
                "delegated skill must run in a fresh-context worker."
            )
        if execution == "inline" and routes_via_spawn:
            errors.append(
                f"{skill_id!r} declares `execution: inline` but its command "
                f"{slash_path.relative_to(REPO_ROOT)} routes through an "
                "`hpc_spawn` Task request or an `hpc-agent run` Bash call. An "
                "inline skill runs in the main conversation — drop the spawn "
                "routing, or mark the skill `delegated`."
            )

    # Skills present on disk but not declared in WORKFLOW_PAIRS or
    # SKILL_ONLY_OK. After the slash-condensation pass, all skills are
    # in SKILL_ONLY_OK by default — they are agent-only surfaces invoked
    # via the Skill tool, not paired 1:1 with user-typed slashes.
    undeclared_skills = skill_ids_present - declared_skills - SKILL_ONLY_OK
    if undeclared_skills:
        errors.append(
            "skill(s) on disk with no entry in WORKFLOW_PAIRS or "
            f"SKILL_ONLY_OK: {sorted(undeclared_skills)}. Add them to "
            "scripts/lint_skill_command_sync.py:SKILL_ONLY_OK (the "
            "default, for agent-only skills) or to WORKFLOW_PAIRS "
            "(only if a paired user-typed slash also ships)."
        )

    # Workflow-trigger slash commands route to the spawn pipeline via
    # ``hpc-agent run <workflow>`` instead of pairing with a Skill —
    # their workflow lives in ``src/hpc_agent/worker_prompts/<name>.md``
    # (see ``docs/internals/skill-policy.md``). Verify each exists, and
    # carries the trigger directive.
    for slash_stem in sorted(WORKFLOW_TRIGGER_SLASHES):
        slash_path = COMMANDS_DIR / f"{slash_stem}.md"
        if not slash_path.is_file():
            errors.append(
                f"declared workflow-trigger slash {slash_stem!r} but "
                f"{slash_path.relative_to(REPO_ROOT)} is missing"
            )
            continue
        body = slash_path.read_text(encoding="utf-8")
        if re.search(r"hpc-agent run\b", body) is None and "hpc-campaign-driver" not in body:
            errors.append(
                f"{slash_path.relative_to(REPO_ROOT)} is a workflow-trigger "
                "slash (WORKFLOW_TRIGGER_SLASHES) but does not shell "
                "`hpc-agent run <workflow>` or `hpc-campaign-driver`. A "
                "trigger slash must route to the code-orchestrated spawn "
                "pipeline; see docs/internals/skill-policy.md."
            )

    # Slash commands without a declared skill, not allow-listed, and not
    # a workflow trigger.
    accounted = declared_slashes | SLASH_ONLY_OK | WORKFLOW_TRIGGER_SLASHES
    undeclared_slashes = slash_ids_present - accounted
    if undeclared_slashes:
        errors.append(
            "slash command(s) on disk with no entry in WORKFLOW_PAIRS, "
            "SLASH_ONLY_OK, or WORKFLOW_TRIGGER_SLASHES: "
            f"{sorted(undeclared_slashes)}"
        )

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(
        f"skills <-> slash_commands in sync "
        f"({len(WORKFLOW_PAIRS)} skill pairs + "
        f"{len(WORKFLOW_TRIGGER_SLASHES)} workflow triggers)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
