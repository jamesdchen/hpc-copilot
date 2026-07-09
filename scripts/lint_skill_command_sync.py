#!/usr/bin/env python3
"""Cross-check ``skills/`` against ``src/hpc_agent/slash_commands/commands/``.

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

* ``src/hpc_agent/slash_commands/skills/<id>/SKILL.md``  ↔
  ``src/hpc_agent/slash_commands/commands/<cmd>.md``
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
SKILLS_DIR = REPO_ROOT / "src" / "hpc_agent" / "slash_commands" / "skills"
COMMANDS_DIR = REPO_ROOT / "src" / "hpc_agent" / "slash_commands" / "commands"

# Each tuple: (skill_id, slash_command_stem). Both files must exist.
#
# Under the three-layer architecture (docs/internals/skill-policy.md):
#   - Interview layer: the slash (human-elicitation)
#   - Decision layer: the workflow skill (agent-autonomous; composes
#     sub-skills, hands off to the execution layer)
#   - Execution layer: worker_prompts/<workflow>.md (deterministic;
#     runs in a fresh-context `claude -p --bare` worker)
#
# Each workflow has both a slash (for human consumers) and a workflow
# skill (for agent consumers). The slash invokes the skill via the
# Skill tool; the skill resolves decisions, then shells out to the
# execution layer. Both surfaces converge on the same execution.
WORKFLOW_PAIRS: list[tuple[str, str]] = [
    ("hpc-submit", "submit-hpc"),
    ("hpc-status", "monitor-hpc"),
    ("hpc-aggregate", "aggregate-hpc"),
    ("hpc-campaign", "campaign-hpc"),
]

# Workflow-trigger slashes that route directly to the spawn pipeline
# without a paired Skill. Empty under the three-layer architecture —
# every slash pairs with a workflow skill; the skill (not the slash)
# is what shells out to ``hpc-agent run <workflow>``.
WORKFLOW_TRIGGER_SLASHES: set[str] = set()

# Skills that ship without a paired slash command. These are the
# sub-skills composed by workflow skills (and by the in-chat agent
# directly when a workflow's interview phase needs a specific
# decision). No slash is needed because users don't type
# `/classify-axis-hpc` directly — they go through `/submit-hpc` which
# composes the sub-skills internally.
SKILL_ONLY_OK: set[str] = {
    "hpc-build-executor",
    "hpc-classify-axis",
    # Notebook-audit loop (2026-07-08): agent-only in-session driver
    # (draft -> lint -> auto-clear -> view -> sign-off -> status); no
    # paired user-typed slash ships in v1.
    "hpc-notebook-audit",
    "hpc-wrap-entry-point",
    # Human-run release procedure (tracked here since 2026-07-04 so it lives
    # under the repo lints; formerly an untracked ~/.claude/skills copy that
    # drifted). No paired slash — invoked as /release via install-commands.
    "release",
}

# Slash-command files allowed to have no skill counterpart. Empty
# under the three-layer architecture — every slash pairs with a skill.
SLASH_ONLY_OK: set[str] = set()


# A workflow command must explicitly route to its skill rather than
# run the workflow from the slash body alone. Under the three-layer
# architecture (slash → skill → execution), the slash's routing is the
# Skill-tool invocation: "Invoke the `<skill>` skill". The slash MUST
# NOT shell out to `hpc-agent run` or `hpc-campaign-driver` itself —
# that's the skill's job, not the slash's.
_INVOKE_DIRECTIVE_RE = re.compile(
    # ``[\`*]{0,2}`` (not ``[\`*]?``) so the directive accepts
    # ``**hpc-submit**`` and ``\`hpc-submit\``` wrapping — bold
    # wrapping is tolerated.
    r"[Ii]nvoke the [`*]{0,2}[a-z][a-z0-9-]+[`*]{0,2} skill"
    r"|subagent[^\n]*?to execute it \(`skills/[a-z0-9-]+/SKILL\.md`\)"
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

# Match an "## Inputs" section's table rows. Each row looks like:
#   | `field_name` | source description |
# Capture group 1 = field name; group 2 = source description.
_INPUTS_ROW_RE = re.compile(r"^\|\s*`([a-z_][a-z0-9_]*)`\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)


def _required_inputs(skill_body: str) -> set[str]:
    """Return the set of field names the skill marks as Required.

    A field is Required when its Source column starts with "Required" (case
    insensitive). Optional fields (auto-resolved, caller-supplied with
    default) are not in this set — the lint doesn't require those to
    appear in the slash body because the slash can correctly omit them.
    """
    in_inputs = False
    required: set[str] = set()
    for line in skill_body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Inputs"):
            in_inputs = True
            continue
        if in_inputs and stripped.startswith("## "):
            break
        if not in_inputs:
            continue
        m = _INPUTS_ROW_RE.match(line)
        if m is None:
            continue
        field, source = m.group(1), m.group(2)
        if source.lower().startswith("required"):
            required.add(field)
    return required


def _check_skill_frontmatter(skill_id: str, skill_path: Path, errors: list[str]) -> str | None:
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

        # Input-shape drift check: every field the skill marks as Required
        # must appear in the slash body (in the invocation pseudo-code,
        # a dialog template, or an args block — anywhere). Optional fields
        # aren't checked — the slash can legitimately omit those and let
        # the skill auto-resolve.
        skill_body = skill_path.read_text(encoding="utf-8")
        required = _required_inputs(skill_body)
        missing = sorted(f for f in required if f not in body)
        if missing:
            errors.append(
                f"{slash_path.relative_to(REPO_ROOT)} does not mention "
                f"required input field(s) of {skill_id!r}: {missing}. "
                "Every field the skill marks as Required in its Inputs "
                "table must appear somewhere in the slash body — either "
                "the invocation pseudo-code, a dialog template, or the "
                "`$ARGUMENTS` parser. Slash and skill input shape drift "
                "is a silent failure mode: the slash invokes without the "
                "field; the skill refuses or auto-resolves to an "
                "unintended default."
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

    # Slash commands without a declared skill and not allow-listed.
    accounted = declared_slashes | SLASH_ONLY_OK | WORKFLOW_TRIGGER_SLASHES
    undeclared_slashes = slash_ids_present - accounted
    if undeclared_slashes:
        errors.append(
            "slash command(s) on disk with no entry in WORKFLOW_PAIRS "
            f"or SLASH_ONLY_OK: {sorted(undeclared_slashes)}"
        )

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(
        f"skills <-> slash_commands in sync "
        f"({len(WORKFLOW_PAIRS)} workflow pairs + "
        f"{len(SKILL_ONLY_OK)} agent-only skills)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
