"""pytest wrapper for ``scripts/lint_skills.py``.

Runs the SKILL.md prose-pattern linter and asserts on rule counts.
Today everything is ``severity="warn"`` and the test is inventory-only
— it produces the per-skill markdown table the WS4 deliverable
specifies and never fails. Promote individual rules to a hard
assertion by lowering the ``MAX_PER_RULE`` ceiling once the violation
count for that rule reaches zero across every skill.

The lint script itself is self-contained (``scripts/lint_skills.py``)
and can also run from the command line for a human-readable report.
This wrapper just gives the lint a slot in the pytest gate so CI (when
unblocked) and the local pre-commit suite both surface drift.

Marked with ``lint`` — run with ``pytest -m lint``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import lint_skills  # noqa: E402

# Per-rule maximum violation counts across the whole skills tree. The
# inventory pass shipped with WS4 sets these to the current observed
# counts plus a small headroom buffer — a regression that adds new
# violations will push past the ceiling and fail. The ceilings are
# author-facing knobs, not policy: lower them whenever the count drops
# and you want the lint to ratchet down.
#
# Verify-a-guard-can-fire (docs/internals/engineering-principles.md):
# every rule above is tested in ``test_lint_rule_fires_on_synthetic_input``
# to confirm the lint actually catches the pattern it claims to.
MAX_PER_RULE: dict[str, int] = {
    "prose-decide": 0,
    "embedded-recovery-menu": 0,
    # 0 today. The four workflow skills (hpc-submit, hpc-status,
    # hpc-aggregate, hpc-campaign) now each carry an explicit "Your final
    # action MUST be a tool call" guard in their driver-loop turn-ending
    # guidance, and none of the passing skills have an un-guarded Return
    # step. Ratcheted 4→0 — any new un-guarded Return section is a real
    # regression.
    "return-without-tool-call-guard": 0,
    "trailing-narration-example": 0,
    # 11 today (hpc-classify-axis 4, hpc-wrap-entry-point 7). Almost
    # every "Resolve X" step is bookkeeping prose for a per-field
    # auto-resolve; the action is implied by the branch bullets below.
    # Drop the ceiling as steps are restructured to either name a
    # primitive call explicitly or formalise the choice as an enumerated
    # ambiguity.
    "step-without-action-ending": 11,
}


pytestmark = pytest.mark.lint


def _current_findings() -> dict[Path, dict[str, list[tuple[int, str]]]]:
    findings: dict[Path, dict[str, list[tuple[int, str]]]] = {}
    for path in lint_skills.collect_skill_files():
        findings[path] = lint_skills.lint_skill_file(path)
    return findings


def test_inventory_report_renders() -> None:
    """The markdown report renders without crashing on the current tree.

    Deliverable item 2: produce the markdown table of violations per
    skill. Asserting the report is well-formed (every rule appears,
    every skill has a row) is the minimum contract.
    """
    findings = _current_findings()
    report = lint_skills.render_report(findings)
    assert "# SKILL.md prose-pattern lint report" in report
    assert "## Per-skill summary" in report
    for rule in lint_skills.RULES:
        assert rule.id in report
    for path in findings:
        assert path.parent.name in report


@pytest.mark.parametrize("rule", lint_skills.RULES, ids=lambda r: r.id)
def test_per_rule_ceiling_not_exceeded(rule: lint_skills.Rule) -> None:
    """Each rule's total violations across all skills stays under its
    ceiling.

    Ceilings are starting values from the inventory pass. Lower the
    entry in ``MAX_PER_RULE`` whenever the count drops; raising it is a
    sign of regression and the author should fix the SKILL.md, not the
    test.
    """
    findings = _current_findings()
    total = sum(len(per_rule.get(rule.id, [])) for per_rule in findings.values())
    ceiling = MAX_PER_RULE[rule.id]
    assert total <= ceiling, (
        f"{rule.id} now has {total} violation(s); ceiling is {ceiling}. "
        f"Run `python scripts/lint_skills.py` for the full report. If "
        f"the violations are legitimate (new exemption), bump the "
        f"ceiling AND document why; otherwise fix the SKILL.md."
    )


def test_lint_rule_fires_on_synthetic_input(tmp_path: Path) -> None:
    """Every rule fires at least once against synthetic input that
    contains a deliberate violation of each pattern.

    This is the "verify a guard can actually fire" check
    (docs/internals/engineering-principles.md): a lint rule with no fire
    path is inertia, not enforcement. Building
    the synthetic skill from the rule definitions and asserting each
    rule_id appears in the findings catches the case where a regex was
    silently broken by an edit.
    """
    synthetic = tmp_path / "SKILL.md"
    synthetic.write_text(
        """---
name: synth
execution: inline
category: agent-autonomous
---

## Execution style

- Be terse.

## Steps

### 1. Free-form decide

Look at the run state and **decide what to do** about it. You may also retry.

### 2. Recovery menu

On failure, choose one of these recovery paths:
1. fix the spec
2. delete and resubmit
3. ignore and continue

### 3. Return envelope

Done.

### 4. Trailing narration

Then write `Returning result to hpc-submit: { ok: true }` to chat.
""",
        encoding="utf-8",
    )
    result = lint_skills.lint_skill_file(synthetic)
    expected_fires = {
        "prose-decide",
        "embedded-recovery-menu",
        "return-without-tool-call-guard",
        "trailing-narration-example",
        "step-without-action-ending",
    }
    missing = expected_fires - set(result)
    assert not missing, (
        f"these rules did not fire on synthetic input: {sorted(missing)}. "
        "Either the rule's regex is broken or the synthetic input no "
        "longer covers it."
    )
