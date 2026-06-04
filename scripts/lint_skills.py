#!/usr/bin/env python3
"""Lint SKILL.md prose for free-form decision content (WS4).

hpc-agent's design philosophy is to convert prose-driven LLM judgment
into CLI-driven enumerated choice (see ``CLAUDE.md`` and the
0.10.0→0.10.6 patch sequence). Every demo-failure cycle has retroactively
patched the relevant SKILL.md after the fact; this lint catches the
common drift modes at author time so regressions don't slip through PR
review.

The gold standard: every numbered Step ends with EITHER

* a ``hpc-agent <verb>`` call (a primitive handoff), OR
* a finite, enumerated choice — the "ambiguity envelope" pattern: caller
  resolves one of N named candidates, no free-form judgement.

This is an **inventory** lint — it emits a markdown table of violations
per skill and exits zero so it can be wired into PR review without
breaking the green-build invariant while the prose backlog clears.
Promote individual rules to ``--fail`` once their violations drop to
zero (see ``RULES`` table; the ``promote_when_zero`` flag is the trip
wire).

Run::

    python scripts/lint_skills.py

CI integration: pytest ``-m lint`` (see ``tests/contract/test_lint_skills.py``).
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "src" / "slash_commands" / "skills"

# ─── Rule definitions ──────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Rule:
    """One lint pattern.

    ``severity``:
        * ``"error"`` — promote to a failing lint when zero (the gold-
          standard pattern is structural and a clean violation is a real
          bug).
        * ``"warn"`` — loose pattern; flag for review but never fail the
          gate. Used for free-form prose patterns where a legitimate
          exemption may exist (e.g. an isolation-ceiling caveat that is
          explanatory, not decisional).

    ``description``: human-readable summary surfaced in the markdown
    report.

    ``check_function``: name of the function in this module that runs
    the check. Each function returns a list of ``(line_no, snippet)``
    tuples — one per violation in one file.
    """

    id: str
    severity: str
    description: str
    check_function: str


RULES: list[Rule] = [
    Rule(
        id="prose-decide",
        severity="error",
        description=(
            "Free-form decision prose inside a Step "
            "(e.g. 'decide what to do', 'consider whether', 'if this "
            "looks wrong, try', 'you may also'). The gold-standard "
            "ending is a primitive call or a finite enumerated choice. "
            "Promoted from 'warn' to 'error' 2026-06-04 — the count was "
            "zero across every skill and the rule fires deterministically; "
            "any new violation is a real regression."
        ),
        check_function="check_prose_decide",
    ),
    Rule(
        id="embedded-recovery-menu",
        severity="warn",
        description=(
            "Embedded numbered-list recovery menu inside an "
            "error-handling context. After WS3 lands these should "
            "reference the central failure-signature registry, not "
            "embed prose recovery steps inline."
        ),
        check_function="check_embedded_recovery_menu",
    ),
    Rule(
        id="return-without-tool-call-guard",
        severity="warn",
        description=(
            "Section titled 'Return ...' / 'Return envelope' with no "
            "nearby 'Final action MUST be a tool call' guard. The "
            "harness fires end-of-turn on any non-tool-call message, "
            "so a Returns step without that explicit guard tends to "
            "induce a closing chat message and the parent skill never "
            "resumes."
        ),
        check_function="check_return_step_guard",
    ),
    Rule(
        id="trailing-narration-example",
        severity="error",
        description=(
            "Example of trailing chat-message narration at sub-skill "
            "boundary (e.g. `Returning <X> to <parent>: { ... }`). "
            "Such examples teach the agent the wrong shape; the "
            "tool-call return IS the envelope. Promoted from 'warn' to "
            "'error' 2026-06-04 — zero violations across every skill; "
            "any new occurrence is a real regression."
        ),
        check_function="check_trailing_narration_example",
    ),
    Rule(
        id="step-without-action-ending",
        severity="warn",
        description=(
            "Numbered Step whose body neither ends with a primitive "
            "call (``hpc-agent <verb>``) nor a finite enumerated choice "
            "(the 'ambiguity envelope' pattern). Either add a primitive "
            "handoff or formalise the choice as a JSON ambiguities "
            "entry."
        ),
        check_function="check_step_ends_in_action",
    ),
]


# ─── Pattern catalogue ────────────────────────────────────────────────────

# Free-form decision prose. Each pattern is anchored with a word
# boundary; case-insensitive at use site. The phrases were sourced from
# the prose-fix commits in the 0.10.1-0.10.6 sequence (88a3869a,
# 8986cf5c, 50a4b61d) — every one of these phrasings appeared in a
# SKILL.md and was retroactively replaced with an enumerated choice.
_PROSE_DECIDE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bdecide what to do\b",
        r"\bconsider whether\b",
        r"\bif this looks wrong,? try\b",
        r"\byou (?:may|might|could) also\b",
        r"\bup to (?:you|the agent)\b",
        r"\bat your discretion\b",
        r"\buse your judgment\b",
        r"\buse your judgement\b",
        r"\bfeel free to\b",
    )
]

# Trailing-narration example. The 2026-06-04 WS3 prose fix added the
# guard line "Writing a closing message like `Returning to ...: { ... }`
# **ends your turn**" — that *describes* the antipattern, so we must
# distinguish description (in a guard block) from a literal example.
# Match a markdown code block / inline-code prose containing the
# `Returning` shape.
_TRAILING_NARRATION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # Match literal example shapes — `Returning X to Y: { ... }` —
        # but NOT the negative-example guard text that uses single
        # backticks to *call out* the pattern as forbidden. We rely on
        # the WS3 guard line carrying the "**ends your turn**" or
        # "Writing a closing message like" marker; the linter walks
        # line-by-line so the surrounding-line check happens in code,
        # not in the regex.
        r"`Returning\s+[^`]*?to\s+[^`]*?:\s*\{[^`]*?\}`",
        r"`Returning\s+to\s+[a-z0-9_-]+:",
    )
]

# Numbered Step heading like ``### 7. Return ambiguities if any`` /
# ``### 6. Return envelope``. We then check whether a tool-call guard
# follows within the section.
_RETURN_STEP_HEADING = re.compile(
    r"^#{2,4}\s+\d+[a-z]?\.\s+Return\b",
    re.IGNORECASE,
)

# Guard phrases that excuse a Return step.
_TOOL_CALL_GUARD_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfinal action MUST be a tool call\b",
        r"\bterminate silently\b",
        r"\bno (?:closing|trailing) message\b",
        r"\bno chat message at skill return\b",
    )
]

# Numbered Step heading at any depth.
_STEP_HEADING = re.compile(
    r"^(#{2,4})\s+(\d+[a-z]?)\.\s+(.+?)\s*$",
)

# A line that looks like an action ending — primitive call or
# enumerated-choice (ambiguity-envelope) shape.
_PRIMITIVE_CALL = re.compile(r"`hpc-agent\s+[a-z][a-z0-9-]*", re.IGNORECASE)
_AMBIGUITIES_BLOCK = re.compile(r'"ambiguities"\s*:', re.IGNORECASE)
_NEEDS_RESOLUTION = re.compile(r"needs_resolution\b", re.IGNORECASE)
_AMBIGUITY_ENTRY = re.compile(r'"field"\s*:.*"candidates"', re.IGNORECASE | re.DOTALL)
_BRANCH_BULLET = re.compile(r"^\s*[-*]\s+\*\*[a-z]", re.IGNORECASE)
_SPEC_INVALID_BRANCH = re.compile(r"\bspec_invalid\b|\bneeds_resolution\b", re.IGNORECASE)

# Embedded recovery menu — a numbered list (1./2./3.) appearing in an
# error-handling context (a paragraph containing branch / fallback /
# remediation language). Authored-recovery-menus are exactly what WS3's
# registry centralizes.
_RECOVERY_CONTEXT = re.compile(
    r"\b(remediation|recover(?:y)?|fallback|on (?:failure|error)|when .* fails?)\b",
    re.IGNORECASE,
)


# ─── Skill-body parsing ───────────────────────────────────────────────────


@dataclasses.dataclass
class StepBlock:
    """One numbered Step in a SKILL.md."""

    start_line: int  # 1-indexed
    end_line: int  # 1-indexed exclusive
    title: str
    body_lines: list[str]

    @property
    def body_text(self) -> str:
        return "\n".join(self.body_lines)


def _split_into_steps(lines: list[str]) -> list[StepBlock]:
    """Split *lines* into numbered Step blocks.

    A Step is everything from one ``### N. …`` heading until the next
    Step heading (or until a top-level ``## …`` section ends the Steps
    block). Non-Step content (Inputs table, Execution style, Notes) is
    not returned.
    """
    blocks: list[StepBlock] = []
    in_steps_section = False
    current: StepBlock | None = None

    for i, raw in enumerate(lines):
        stripped = raw.rstrip("\n")
        # Track entry into / exit from the ``## Steps`` section. Steps
        # live under ``## Steps``; anything else (Inputs, Execution
        # style, Notes) is out of scope for the step-ending lint.
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            if current is not None:
                current.end_line = i + 1
                blocks.append(current)
                current = None
            in_steps_section = heading == "steps"
            continue
        if not in_steps_section:
            continue
        step_match = _STEP_HEADING.match(stripped)
        if step_match:
            if current is not None:
                current.end_line = i + 1
                blocks.append(current)
            current = StepBlock(
                start_line=i + 1,
                end_line=len(lines) + 1,
                title=step_match.group(3).strip(),
                body_lines=[stripped],
            )
            continue
        if current is not None:
            current.body_lines.append(stripped)

    if current is not None:
        blocks.append(current)
    return blocks


def _strip_html_comments(text: str) -> str:
    """Strip ``<!-- ... -->`` so guard markers / decision-content blocks
    don't trigger pattern matches on their narration."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _strip_fenced_code_blocks(text: str) -> str:
    """Strip ``` fenced blocks. JSON examples inside Step bodies
    legitimately contain the words ``decide`` etc.; the lint targets the
    *prose* surrounding them."""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


# ─── Rule check functions ────────────────────────────────────────────────


def check_prose_decide(path: Path, lines: list[str]) -> list[tuple[int, str]]:
    """``prose-decide``: flag free-form decision prose in any Step.

    Body-text only — Execution-style block + Notes section are out of
    scope (they legitimately contain phrases like "your judgement is
    needed" for explanatory purposes; the lint targets prose that
    drives a Step's *resolution*).
    """
    violations: list[tuple[int, str]] = []
    for step in _split_into_steps(lines):
        cleaned = _strip_fenced_code_blocks(_strip_html_comments(step.body_text))
        for offset, raw in enumerate(cleaned.splitlines()):
            for pat in _PROSE_DECIDE_PATTERNS:
                m = pat.search(raw)
                if m:
                    line_no = step.start_line + offset
                    violations.append((line_no, raw.strip()[:120]))
                    break
    return violations


def check_embedded_recovery_menu(path: Path, lines: list[str]) -> list[tuple[int, str]]:
    """``embedded-recovery-menu``: numbered recovery list in a paragraph
    whose context names remediation / fallback / on-failure.

    A bullet list of ``- **terminal** / **abandoned** / ...`` lifecycle
    branches is *not* a recovery menu — it's an enumerated choice
    against a typed scheduler state, the gold-standard pattern. The
    recovery-menu shape is a free-form prose list of fixes the agent
    must pick from.
    """
    violations: list[tuple[int, str]] = []
    for step in _split_into_steps(lines):
        cleaned = _strip_fenced_code_blocks(_strip_html_comments(step.body_text))
        body_lines = cleaned.splitlines()
        for i, line in enumerate(body_lines):
            # A markdown numbered-list item: "1. foo" / "2. bar".
            m = re.match(r"^\s*(\d+)\.\s+(.+)", line)
            if not m or m.group(1) != "1":
                continue
            # Look back for a recovery-context phrase within the prior
            # 4 lines.
            window = "\n".join(body_lines[max(0, i - 4) : i])
            if not _RECOVERY_CONTEXT.search(window):
                continue
            # Confirm 2. follows within the next 6 lines (a single "1."
            # is a single recovery hint, not a menu).
            next_window = "\n".join(body_lines[i : i + 8])
            if re.search(r"^\s*2\.\s+", next_window, re.MULTILINE):
                line_no = step.start_line + i
                violations.append((line_no, line.strip()[:120]))
    return violations


def check_return_step_guard(path: Path, lines: list[str]) -> list[tuple[int, str]]:
    """``return-without-tool-call-guard``: Return step missing tool-call
    guard nearby.

    The WS3 prose fix added the guard at the Execution-style block.
    If a Return step exists and *neither* the Execution-style block
    nor the Return step body name the guard, flag it.
    """
    body = "\n".join(lines)
    body_no_comments = _strip_html_comments(body)
    has_global_guard = any(p.search(body_no_comments) for p in _TOOL_CALL_GUARD_PATTERNS)
    violations: list[tuple[int, str]] = []
    for step in _split_into_steps(lines):
        if not _RETURN_STEP_HEADING.match(step.body_lines[0] if step.body_lines else ""):
            continue
        if has_global_guard:
            continue
        # No local guard either → flag.
        if not any(p.search(step.body_text) for p in _TOOL_CALL_GUARD_PATTERNS):
            violations.append((step.start_line, step.body_lines[0].strip()[:120]))
    return violations


def check_trailing_narration_example(path: Path, lines: list[str]) -> list[tuple[int, str]]:
    """``trailing-narration-example``: literal example of trailing chat-
    message narration like ``Returning … to <parent>: { … }``.

    The WS3 prose fix added the antipattern *as a negative example* in
    the Execution-style block — distinguishable because the surrounding
    text says "ends your turn" or "Writing a closing message like". A
    literal example without that warning context is the bug.
    """
    violations: list[tuple[int, str]] = []
    for i, raw in enumerate(lines):
        for pat in _TRAILING_NARRATION_PATTERNS:
            if pat.search(raw):
                # Check surrounding context (±2 lines) for the
                # warn-context phrase. The WS3 fix wraps the example
                # with explicit "ends your turn" / "Writing a closing
                # message like" markers.
                window = "\n".join(lines[max(0, i - 2) : i + 3])
                if re.search(
                    r"ends your turn|Writing a closing message like|antipattern|"
                    r"NEVER write|do not (?:write|emit)",
                    window,
                    re.IGNORECASE,
                ):
                    break
                violations.append((i + 1, raw.strip()[:120]))
                break
    return violations


def check_step_ends_in_action(path: Path, lines: list[str]) -> list[tuple[int, str]]:
    """``step-without-action-ending``: a numbered Step that ends without
    either a primitive call or an enumerated-choice block.

    Counts as an action-ending if the Step body contains:

    * any ``hpc-agent <verb>`` invocation (the primitive-handoff
      ending), OR
    * a JSON ``ambiguities`` list / ``needs_resolution`` envelope (the
      enumerated-choice ending), OR
    * a JSON ``"field"…"candidates"`` ambiguity entry, OR
    * a bulleted enumeration of branch outcomes (``- **terminal**`` …)
      tied to a ``spec_invalid`` / ``needs_resolution`` decision, OR
    * a ``Skill`` tool invocation (sub-skill compose).

    Pure narration / re-export / "Return envelope" placeholder steps
    are intentionally excluded — they are bookkeeping headings.
    """
    violations: list[tuple[int, str]] = []
    # Bookkeeping step titles whose body is intentionally narrative (the
    # action is implied by branch bullets that enumerate a finite outcome
    # set — terminal/abandoned/in-flight, caller-supplied/auto-resolve/
    # ambiguity, etc.). Expanded 2026-06-04 after the WS4 audit's
    # `step-without-action-ending` rule over-fired on 29 such headings
    # whose semantics were finite-enumerated despite missing a literal
    # `hpc-agent <verb>` line. WS4 design question 3 verdict: refine the
    # rule rather than accept the noise.
    # ``\b`` (word boundary) after the Resolve sub-alternation prevents
    # over-match: ``Resolve cluster_ssh_echo`` should NOT be treated as
    # bookkeeping for "Resolve cluster", because the cluster_ssh_echo
    # Step has its own action ending. The ``_`` is a word character so
    # ``\b`` requires a non-word-character right after the alternative.
    bookkeeping = re.compile(
        r"^(?:Return ambiguities if any|Return envelope|Return the .+|"
        r"Propagate worker ambiguities|Hand off|Build the fields json|"
        r"Ensure agent assets installed|"
        r"Resolve\s+(?:cluster|run_id|run|data axis|homogeneous axes|"
        r"walltime|gpu_type|partition|task_generator|entry point|"
        r"frozen_configs|ambiguities if any)\b|"
        r"Pre-fill from memory|Cache check|Detect or scaffold|"
        r"Identify the run|Cover non-axis|Skip if caller supplied|"
        r"Try the cheap match|Branch on)",
        re.IGNORECASE,
    )
    for step in _split_into_steps(lines):
        if not step.body_lines:
            continue
        title = step.title
        if bookkeeping.match(title):
            continue
        text = step.body_text
        cleaned = _strip_html_comments(text)
        if _PRIMITIVE_CALL.search(cleaned):
            continue
        if _AMBIGUITIES_BLOCK.search(cleaned) or _NEEDS_RESOLUTION.search(cleaned):
            continue
        if _AMBIGUITY_ENTRY.search(cleaned):
            continue
        if _SPEC_INVALID_BRANCH.search(cleaned) and _BRANCH_BULLET.search(cleaned):
            continue
        if re.search(r"\bSkill\b.*\bsub-skill\b|invoke the [`*]{0,2}hpc-", cleaned, re.IGNORECASE):
            continue
        violations.append((step.start_line, f"### {title}"[:120]))
    return violations


# ─── Driver ───────────────────────────────────────────────────────────────


_CheckFunc = Callable[[Path, list[str]], list[tuple[int, str]]]

CHECK_BY_ID: dict[str, _CheckFunc] = {
    "prose-decide": check_prose_decide,
    "embedded-recovery-menu": check_embedded_recovery_menu,
    "return-without-tool-call-guard": check_return_step_guard,
    "trailing-narration-example": check_trailing_narration_example,
    "step-without-action-ending": check_step_ends_in_action,
}


def lint_skill_file(path: Path) -> dict[str, list[tuple[int, str]]]:
    """Run every rule against *path*. Returns ``{rule_id: [(line, snippet), …]}``."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: dict[str, list[tuple[int, str]]] = {}
    for rule in RULES:
        check = CHECK_BY_ID[rule.id]
        violations = check(path, lines)
        if violations:
            out[rule.id] = violations
    return out


def collect_skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def render_report(
    findings: dict[Path, dict[str, list[tuple[int, str]]]],
) -> str:
    """Render the markdown report described in the deliverable."""
    rows: list[str] = []
    rows.append("# SKILL.md prose-pattern lint report\n")
    rows.append(
        "Inventory pass — every row is a violation of the gold-standard "
        "prose pattern (every Step ends in either a `hpc-agent <verb>` "
        "call or a finite enumerated choice). Severity is `warn` for all "
        "rules today; promote to `error` once a rule's count reaches "
        "zero across every skill.\n"
    )

    # Per-skill summary table.
    rows.append("## Per-skill summary\n")
    rows.append("| Skill | " + " | ".join(r.id for r in RULES) + " | total |")
    rows.append("|---|" + "---|" * (len(RULES) + 1))
    for path in sorted(findings):
        skill = path.parent.name
        per_rule = findings[path]
        counts = [str(len(per_rule.get(r.id, []))) for r in RULES]
        total = sum(len(v) for v in per_rule.values())
        rows.append(f"| {skill} | " + " | ".join(counts) + f" | {total} |")
    rows.append("")

    # Per-rule details.
    rows.append("## Per-rule detail\n")
    for rule in RULES:
        rows.append(f"### `{rule.id}` ({rule.severity})\n")
        rows.append(f"{rule.description}\n")
        any_violation = False
        for path in sorted(findings):
            violations = findings[path].get(rule.id, [])
            if not violations:
                continue
            any_violation = True
            skill = path.parent.name
            rows.append(f"**{skill}** ({len(violations)})")
            rows.append("")
            for line_no, snippet in violations:
                snippet_md = snippet.replace("|", r"\|")
                rows.append(f"- L{line_no}: `{snippet_md}`")
            rows.append("")
        if not any_violation:
            rows.append("_no violations_\n")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help=(
            "Exit non-zero if any `error`-severity rule fires. Default is "
            "inventory-only (always exit 0)."
        ),
    )
    parser.add_argument(
        "--write",
        type=Path,
        default=None,
        help="Write the markdown report to this path (default: stdout).",
    )
    args = parser.parse_args(argv)

    findings: dict[Path, dict[str, list[tuple[int, str]]]] = {}
    for path in collect_skill_files():
        result = lint_skill_file(path)
        findings[path] = result

    report = render_report(findings)
    if args.write is not None:
        args.write.write_text(report, encoding="utf-8")
        print(f"wrote {args.write}", file=sys.stderr)
    else:
        sys.stdout.write(report)

    if args.fail_on_error:
        for per_rule in findings.values():
            for rule in RULES:
                if rule.severity == "error" and per_rule.get(rule.id):
                    return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
