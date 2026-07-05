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
    POLLING_REASON_UNKNOWN_EXHAUSTED,
    SETTLE_REASON_ABANDONED,
    SETTLE_REASON_COMPLETE,
    SETTLE_REASON_FAILED,
    UNKNOWN_TICKS_BEFORE_ESCALATION,
    all_tasks_complete,
    classify_polling,
    classify_settled,
    run_failed,
    settle,
    unresolved_unknown,
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


# --------------------------------------------------------------------------
# Bounded-unknown escalation (proving run #3, finding f): a vanished remote
# workdir left the poll loop classifying "unknown" forever — no live work, no
# results, no failure evidence, and no arm that ever terminated. The
# classifier now escalates to a terminal ``abandoned`` anomaly once the
# caller-tracked unresolved-unknown streak crosses the bound.
# --------------------------------------------------------------------------


def test_unresolved_unknown_predicate():
    # The tick signature: no live work, not complete, at least one unknown.
    assert unresolved_unknown(_summary(unknown=5), 5) is True
    assert unresolved_unknown(_summary(complete=3, unknown=2), 5) is True
    # Live work, positive evidence, or completion → not unresolved.
    assert unresolved_unknown(_summary(unknown=4, running=1), 5) is False
    assert unresolved_unknown(_summary(unknown=4, pending=1), 5) is False
    assert unresolved_unknown(_summary(unknown=4, failed=1), 5) is False
    assert unresolved_unknown(_summary(complete=5), 5) is False
    # No unknowns at all → nothing unresolved.
    assert unresolved_unknown(_summary(), 5) is False
    # Zero-task runs and reporter-failure summaries carry no evidence.
    assert unresolved_unknown(_summary(unknown=5), 0) is False
    assert unresolved_unknown({"error": "boom"}, 5) is False


def test_polling_unknown_below_bound_keeps_polling():
    s = _summary(unknown=5)
    for streak in range(UNKNOWN_TICKS_BEFORE_ESCALATION):
        assert classify_polling(s, 5, unknown_streak=streak) == (None, None)


def test_polling_unknown_streak_escalates_to_abandoned_anomaly():
    state, reason = classify_polling(
        _summary(unknown=5), 5, unknown_streak=UNKNOWN_TICKS_BEFORE_ESCALATION
    )
    assert state == LifecycleState.ABANDONED
    assert reason is not None
    assert reason.startswith(POLLING_REASON_UNKNOWN_EXHAUSTED)


def test_polling_streak_alone_never_escalates_a_live_tick():
    # A stale (high) streak must not fire unless the CURRENT tick is still
    # unresolved-unknown — live work or a complete count wins.
    assert classify_polling(_summary(unknown=2, running=1), 5, unknown_streak=99) == (None, None)
    assert classify_polling(_summary(unknown=2, pending=3), 5, unknown_streak=99) == (None, None)
    assert classify_polling(_summary(complete=5), 5, unknown_streak=99) == (
        LifecycleState.COMPLETE,
        None,
    )


def test_polling_failure_evidence_outranks_unknown_escalation():
    # Positive failure evidence classifies FAILED even at a maxed-out streak.
    state, reason = classify_polling(_summary(failed=1, unknown=4), 5, unknown_streak=99)
    assert state == LifecycleState.FAILED
    assert reason == "failed_tasks_no_auto_recover_in_mvp"
