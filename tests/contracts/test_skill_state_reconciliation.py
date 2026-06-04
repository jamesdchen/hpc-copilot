"""Prose-contract guards for the two state-reconciliation skill fixes.

Skills are LLM-facing procedures, so the verification here is that the
load-bearing guidance is present (and named with the exact error codes /
verbs callers branch on) — the same drift-guard philosophy as
``test_lint_skill_md_literal_drift``. These fail loudly if a future edit
drops the reconcile branch (#248) or the task_generator-mismatch guard
(#247), the way both gaps originally shipped.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS = REPO_ROOT / "src" / "slash_commands" / "skills"


def _read(skill: str) -> str:
    return (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")


def test_aggregate_skill_reconciles_before_nothing_to_aggregate():
    # #248: the aggregate skill must reconcile a journal-only in-flight run
    # against the cluster before refusing, and surface an abandoned run as
    # run_abandoned rather than an indefinite "nothing to aggregate".
    text = _read("hpc-aggregate")
    assert "hpc-agent reconcile" in text, "aggregate skill must call `hpc-agent reconcile`"
    assert "run_abandoned" in text, "aggregate skill must surface spec_invalid: run_abandoned"
    assert "lifecycle_state" in text, "aggregate skill must branch on reconcile's lifecycle_state"
    # The trigger condition the bug missed: trusting next_step_hint == monitor.
    assert "next_step_hint" in text and "in_flight" in text
    # Symmetric to the submit path's existing reconcile.
    assert "abandoned" in text


def test_aggregate_nothing_to_aggregate_is_cluster_confirmed():
    # The bare-journal "zero terminal runs → nothing_to_aggregate" return must
    # be gated on the reconcile step, not the journal alone.
    text = _read("hpc-aggregate")
    # The reconcile step is referenced from the resolve-run decision.
    assert "Step 1b" in text
    assert (
        "confirmed against the cluster" in text.lower() or "confirmed against the cluster" in text
    )


def test_submit_skill_guards_task_generator_mismatch():
    # #247: a cached interview.json must not silently override a divergent
    # caller-supplied task_generator (the 8-vs-100 drift).
    text = _read("hpc-submit")
    assert "task_generator_mismatch" in text, "submit skill must surface task_generator_mismatch"
    assert "on_task_generator_mismatch" in text, "submit skill must document the mismatch field"
    # All three documented behaviours must be named.
    for behaviour in ("fail", "refresh", "prefer-caller"):
        assert behaviour in text, (
            f"submit skill must document on_task_generator_mismatch={behaviour}"
        )
    # The guard sits at the interview.json short-circuit.
    assert "interview.json" in text
    # `fail` is the default (loud), not silent.
    assert "default" in text.lower()
