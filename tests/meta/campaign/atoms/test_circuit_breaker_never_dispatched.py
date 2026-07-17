"""Circuit-breaker attribution: never-dispatched submits are a SEPARATE streak.

The F1 twin (2026-07-17 transport/monitor/campaign review, Finding 1). A
submit-once safe-resubmit (``reconcile._safe_resubmit``) stamps
``submitting -> abandoned`` with
``last_status.verdict_reason == NEVER_DISPATCHED_VERDICT_REASON`` for a child
whose array NEVER entered the scheduler — a dispatch-window / control-plane
infra event, zero tasks ran. Counting it as a genuine consecutive iteration
failure let sustained control-plane flapping trip ``stop_circuit_breaker`` with
a rationale MISATTRIBUTING the infra fault to experiment failures.

The fix keeps loop safety (a stuck dispatch loop still halts) but splits the
attribution into two streaks so the halt rationale is HONEST:

* ``count`` / ``run_ids`` — genuine iteration failures.
* ``never_dispatched_count`` / ``never_dispatched_run_ids`` — never-dispatched
  abandons (control-plane fault, no iteration executed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent._kernel.contract.vocabulary import NEVER_DISPATCHED_VERDICT_REASON
from hpc_agent.meta.campaign.atoms.advance import campaign_advance
from hpc_agent.meta.campaign.atoms.circuit_breaker import consecutive_terminal_failures
from hpc_agent.state.index import find_runs_by_campaign
from tests.meta.campaign.atoms.test_circuit_breaker import _seed_iteration

if TYPE_CHECKING:
    from pathlib import Path


def _seed_never_dispatched(experiment_dir: Path, *, run_id: str, campaign_id: str) -> None:
    """Seed the exact abandon reconcile's safe-resubmit stamps (never-dispatched)."""
    _seed_iteration(
        experiment_dir,
        run_id=run_id,
        campaign_id=campaign_id,
        status="abandoned",
        last_status={
            "verdict": "abandoned",
            "verdict_reason": NEVER_DISPATCHED_VERDICT_REASON,
            "recovery_note": "jobmap dir absent and announce census absent",
        },
    )


# ─── streak split at the atom level ─────────────────────────────────────────


def test_never_dispatched_abandon_is_a_separate_streak(journal_home: Path, tmp_path: Path) -> None:
    # Three never-dispatched abandons at the tail must NOT count as iteration
    # failures — they land in the never_dispatched streak instead.
    _seed_never_dispatched(tmp_path, run_id="n0", campaign_id="A")
    _seed_never_dispatched(tmp_path, run_id="n1", campaign_id="A")
    _seed_never_dispatched(tmp_path, run_id="n2", campaign_id="A")

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 0
    assert out["run_ids"] == []
    assert out["never_dispatched_count"] == 3
    assert out["never_dispatched_run_ids"] == ["n2", "n1", "n0"]  # newest-first
    assert out["last_status"] == "abandoned"


def test_plain_abandoned_without_reason_is_still_an_iteration_failure(
    journal_home: Path, tmp_path: Path
) -> None:
    # An ``abandoned`` run with NO never-dispatched verdict_reason stays a
    # genuine iteration failure (the historical behaviour — a truly-vanished
    # run IS an experiment failure).
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="abandoned")

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 2
    assert out["never_dispatched_count"] == 0


def test_mix_resets_neither_streak(journal_home: Path, tmp_path: Path) -> None:
    # A MIX at the tail: each streak SKIPS PAST the other type (never a reset).
    # Tail newest-first: [failed, never_dispatched, failed] ->
    #   iteration streak = 2 (the two failures, nd skipped past)
    #   never_dispatched streak = 1 (nd, failures skipped past)
    # Neither type resets the other's streak to zero.
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_never_dispatched(tmp_path, run_id="n1", campaign_id="A")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="failed")

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 2
    assert out["run_ids"] == ["r2", "r0"]  # newest-first, nd skipped
    assert out["never_dispatched_count"] == 1
    assert out["never_dispatched_run_ids"] == ["n1"]


def test_complete_resets_both_streaks(journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_never_dispatched(tmp_path, run_id="n1", campaign_id="A")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="complete")

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 0
    assert out["never_dispatched_count"] == 0
    assert out["last_status"] == "complete"


def test_submitting_orphan_neither_breaks_nor_extends_either_streak(
    journal_home: Path, tmp_path: Path
) -> None:
    # The landed F1 pin, widened: a ``submitting`` orphan at the tail is skipped
    # for BOTH streaks (it neither disarms the iteration breaker nor extends the
    # never-dispatched one).
    _seed_never_dispatched(tmp_path, run_id="n0", campaign_id="A")
    _seed_never_dispatched(tmp_path, run_id="n1", campaign_id="A")
    _seed_iteration(tmp_path, run_id="s2", campaign_id="A", status="submitting")

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 0
    assert out["never_dispatched_count"] == 2
    assert out["never_dispatched_run_ids"] == ["n1", "n0"]


# ─── end-to-end: campaign-advance honest rationale ──────────────────────────


def test_advance_iteration_failure_stop_keeps_historical_rationale(
    journal_home: Path, tmp_path: Path
) -> None:
    # 3 genuine failures + threshold -> the UNCHANGED iteration-failure stop.
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="failed")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=3)
    assert out["decision"] == "stop_circuit_breaker"
    assert "consecutive iteration failure(s)" in out["reason"]
    assert "never-dispatched" not in out["reason"]
    brief = out["anomaly_brief"]
    assert brief is not None
    assert brief["breaker_kind"] == "iteration_failure"
    assert brief["evidence"]["count"] == 3
    assert brief["evidence"]["run_ids"] == ["r2", "r1", "r0"]


def test_advance_never_dispatched_stop_has_honest_rationale(
    journal_home: Path, tmp_path: Path
) -> None:
    # 3 never-dispatched abandons + threshold -> the never-dispatched stop with
    # the HONEST control-plane rationale. RED on the pre-fix code: it stopped
    # with the WRONG "N consecutive iteration failure(s)" rationale (the abandons
    # counted as genuine failures). Pin the rationale text.
    _seed_never_dispatched(tmp_path, run_id="n0", campaign_id="A")
    _seed_never_dispatched(tmp_path, run_id="n1", campaign_id="A")
    _seed_never_dispatched(tmp_path, run_id="n2", campaign_id="A")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=3)
    assert out["decision"] == "stop_circuit_breaker"
    # The truthful rationale: control-plane fault, NOT experiment failures.
    assert "consecutive never-dispatched submit(s)" in out["reason"]
    assert "control-plane/dispatch fault" in out["reason"]
    assert "check cluster connectivity/reconcile" in out["reason"]
    assert "iteration failure" not in out["reason"]

    breaker = out["circuit_breaker"]
    assert breaker["count"] == 0  # zero genuine iteration failures
    assert breaker["never_dispatched_count"] == 3

    brief = out["anomaly_brief"]
    assert brief is not None
    assert brief["breaker_kind"] == "never_dispatched"
    assert brief["evidence"]["count"] == 3
    assert brief["evidence"]["run_ids"] == ["n2", "n1", "n0"]
    assert "NOT an experiment failure" in brief["recommendation"]


def test_advance_mix_below_threshold_neither_fires(journal_home: Path, tmp_path: Path) -> None:
    # A mix where NEITHER streak reaches the threshold must not halt: the mix
    # must not let the two half-streaks add up into a false trip.
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_never_dispatched(tmp_path, run_id="n1", campaign_id="A")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="failed")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=3)
    assert out["decision"] == "continue"


def test_advance_never_dispatched_under_threshold_continues(
    journal_home: Path, tmp_path: Path
) -> None:
    _seed_never_dispatched(tmp_path, run_id="n0", campaign_id="A")
    _seed_never_dispatched(tmp_path, run_id="n1", campaign_id="A")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=3)
    assert out["decision"] == "continue"
    assert out["circuit_breaker"]["never_dispatched_count"] == 2
