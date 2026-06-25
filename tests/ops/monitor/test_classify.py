"""Pins the single count-to-verdict classifier (`ops.monitor.classify`).

Two things are nailed down here:

* each classifier reproduces the inline logic it replaced, arm for arm, and
* the precedence rule that the abandoned-vs-failed bug (#351) kept violating
  is now a mechanized property, not a discipline re-asserted per commit:
  on the settle path, positive failure evidence outranks absence and strict
  completion outranks failure.

The lenient (polling) vs strict (settled) completion divergence is
intentional; the cross-classifier test below pins it so it stays a conscious
contract rather than drifting silently.
"""

from __future__ import annotations

import pytest

from hpc_agent._kernel.contract.vocabulary import JournalStatus, LifecycleState
from hpc_agent.ops.monitor.classify import (
    SETTLE_REASON_ABANDONED,
    SETTLE_REASON_COMPLETE,
    SETTLE_REASON_FAILED,
    all_tasks_complete,
    classify_polling,
    classify_settled,
    run_failed,
    settle,
)


def _summary(complete=0, running=0, pending=0, failed=0, unknown=0):
    return {
        "complete": complete,
        "running": running,
        "pending": pending,
        "failed": failed,
        "unknown": unknown,
    }


# --------------------------------------------------------------------------
# classify_polling — reproduces the former `_is_terminal`, arm for arm.
# --------------------------------------------------------------------------


def test_polling_in_flight_returns_none():
    assert classify_polling(_summary(complete=2, running=3), 5) == (None, None)


def test_polling_complete_when_count_reaches_total():
    assert classify_polling(_summary(complete=5), 5) == (LifecycleState.COMPLETE, None)


def test_polling_complete_is_lenient_overshoot():
    # complete > total (e.g. a retried task counted twice) still terminal-complete.
    assert classify_polling(_summary(complete=6), 5) == (LifecycleState.COMPLETE, None)


def test_polling_failed_when_work_settled_with_failures():
    state, reason = classify_polling(_summary(complete=2, failed=3), 5)
    assert state == LifecycleState.FAILED
    assert reason == "failed_tasks_no_auto_recover_in_mvp"


def test_polling_still_in_flight_while_work_remains_even_with_failures():
    # pending/running > 0 means not settled — no verdict yet.
    assert classify_polling(_summary(complete=1, failed=1, pending=3), 5) == (None, None)


def test_polling_partial_ok_promotes_partial_success_to_complete():
    state, reason = classify_polling(_summary(complete=2, failed=3), 5, partial_ok=True)
    assert state == LifecycleState.COMPLETE
    assert reason == "partial_ok_with_failures"


def test_polling_partial_ok_zero_success_still_failed():
    state, reason = classify_polling(_summary(complete=0, failed=5), 5, partial_ok=True)
    assert state == LifecycleState.FAILED
    assert reason == "failed_tasks_no_auto_recover_in_mvp"


# --------------------------------------------------------------------------
# classify_settled — reproduces reconcile's three-way verdict.
# --------------------------------------------------------------------------


def test_settled_complete_requires_strict_all_done():
    assert classify_settled(_summary(complete=5), 5) == LifecycleState.COMPLETE


def test_settled_complete_is_strict_not_lenient():
    # A leftover non-complete bucket disqualifies completion on the settle path
    # (unlike polling). Here: nothing failed but one task still 'unknown'.
    assert classify_settled(_summary(complete=5, unknown=1), 5) == LifecycleState.ABANDONED


def test_settled_failed_on_positive_failure_evidence():
    assert classify_settled(_summary(complete=4, failed=1), 5) == LifecycleState.FAILED


def test_settled_abandoned_when_no_evidence():
    # Incomplete, nothing failed → no on-disk evidence at all.
    assert classify_settled(_summary(complete=3, unknown=2), 5) == LifecycleState.ABANDONED


# --------------------------------------------------------------------------
# Precedence property — the mechanized form of the discipline #351 kept
# re-asserting: on the settle path, failure outranks absence and strict
# completion is never claimed while a failure is present.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("failed", [1, 2, 7])
@pytest.mark.parametrize("complete", [0, 3, 5])
def test_settled_failure_outranks_absence(complete, failed):
    total = 5
    verdict = classify_settled(_summary(complete=complete, failed=failed), total)
    # Any positive failure count → never abandoned, never complete.
    assert verdict == LifecycleState.FAILED


def test_settled_never_complete_while_failure_present():
    # Even a full complete count cannot read as COMPLETE if a task failed
    # (strict completion requires every non-complete bucket at zero).
    assert classify_settled(_summary(complete=5, failed=2), 5) != LifecycleState.COMPLETE


def test_settled_verdicts_are_valid_journal_statuses():
    for summary in (_summary(complete=5), _summary(failed=1), _summary(pending=5)):
        assert str(classify_settled(summary, 5)) in set(JournalStatus)


# --------------------------------------------------------------------------
# settle() — verdict provenance. classify_settled stays the verdict-only view.
# --------------------------------------------------------------------------


def test_settle_carries_reason_and_evidence_for_each_arm():
    cases = [
        (_summary(complete=5), LifecycleState.COMPLETE, SETTLE_REASON_COMPLETE),
        (_summary(complete=4, failed=1), LifecycleState.FAILED, SETTLE_REASON_FAILED),
        (_summary(complete=3, unknown=2), LifecycleState.ABANDONED, SETTLE_REASON_ABANDONED),
    ]
    for summary, verdict, reason in cases:
        d = settle(summary, 5)
        assert d.verdict == verdict
        assert d.reason == reason
        # Evidence snapshot is the counts the decision was made from.
        assert d.evidence["total_tasks"] == 5
        assert d.evidence["complete"] == int(summary.get("complete", 0))


def test_classify_settled_matches_settle_verdict():
    # The thin wrapper must never diverge from settle()'s verdict.
    for summary in (_summary(complete=5), _summary(failed=2), _summary(pending=5), _summary()):
        assert classify_settled(summary, 5) == settle(summary, 5).verdict


# --------------------------------------------------------------------------
# The deliberate divergence between the two classifiers, pinned.
# --------------------------------------------------------------------------


def test_polling_and_settled_diverge_on_complete_with_stale_failure():
    # Same counts, different context → intentionally different verdict.
    summary = _summary(complete=5, failed=2)
    assert classify_polling(summary, 5) == (LifecycleState.COMPLETE, None)  # lenient
    assert classify_settled(summary, 5) == LifecycleState.FAILED  # strict


# --------------------------------------------------------------------------
# Predicate edge cases (carried over from the reconcile docstrings).
# --------------------------------------------------------------------------


def test_all_tasks_complete_zero_task_run_is_not_complete():
    assert all_tasks_complete(_summary(complete=0), 0) is False


def test_all_tasks_complete_reporter_error_summary_is_not_complete():
    assert all_tasks_complete({"error": "boom"}, 5) is False


def test_all_tasks_complete_missing_keys_count_as_zero():
    assert all_tasks_complete({"complete": 5}, 5) is True


def test_run_failed_reporter_error_summary_is_false():
    assert run_failed({"error": "boom"}) is False
