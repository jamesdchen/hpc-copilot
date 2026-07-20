"""The reporter-backed settle arm: STRICT all-complete settles without liveness.

Drill-forensics latency fix (a drill run's journal row sat ``in_flight`` for
2h31m after the run completed cluster-side): the reporter-backed settle arm was
gated on ``not alive`` — even a STRICT ``all_tasks_complete(summary, total)``
reporter summary (every task's result proven on disk, zero
running/pending/failed/unknown) could not settle while the scheduler still
showed the job (Slurm COMPLETING / squeue propagation lag). The announce fast
path (``_settle_from_announcements``) was already alive-INDEPENDENT on the same
per-task-evidence logic — the two settle arms disagreed on whether scheduler
liveness is authoritative. They now agree: positive STRICT per-task disk
evidence settles ``complete`` regardless of scheduler lag, and the override is
disclosed via ``last_status.scheduler_alive_at_settle`` (never silent).

The early-settle class stays closed by construction: ONLY the strict shape
crosses the liveness line — any running/pending (a retry/requeue in flight),
any failed (even a stale bucket; the lenient-vs-strict divergence pinned in
``test_classify``), or any unknown keeps the run in_flight, and the
``alive_check_failed`` / ``reporter_failed`` probe gates are unchanged.

Cluster-free: the three SSH fan-out calls are monkeypatched; the harvest is a
counting fake that writes the real receipt (so the receipt-derived idempotency
holds for the right reason, mirroring the sibling reconcile test doubles).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.monitor import reconcile as recon
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, total_tasks: int = 4, job_ids=("100", "200")) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=total_tasks,
        submitted_at="2026-07-11T00:00:00Z",
        experiment_dir="/exp",
        status="in_flight",
    )


def _count_harvests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _fake(experiment_dir, run_id, *, terminal_cause, record=None, **_kw):
        calls.append(terminal_cause)
        # Mirror the real guard's LAST step: append a durable receipt to the
        # ledger, so the reconcile backstop's receipt-derived idempotency holds
        # (a real harvest ALWAYS writes a marker — a receipt-less fake would let
        # the terminal-with-no-receipt backstop re-fire on every re-reconcile).
        append_jsonl_line(
            harvest_marker_path(experiment_dir, run_id),
            {"run_id": run_id, "terminal_cause": terminal_cause, "harvest_ok": True},
        )
        return {}

    monkeypatch.setattr(recon, "harvest_on_terminal", _fake)
    return calls


def _stub_reporter(monkeypatch: pytest.MonkeyPatch, summary: dict[str, int]) -> None:
    """Reporter walk returns *summary*; no waves. Liveness is stubbed per test."""
    monkeypatch.setattr(
        recon,
        "_ssh_status_report",
        lambda **_kw: {"summary": dict(summary), "waves": {}},
    )
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])


def test_strict_all_complete_settles_complete_while_scheduler_still_shows_job(
    tmp_path, monkeypatch
):
    """The drill-forensics case: every task's result is PROVEN on disk (strict
    all-complete) but the scheduler still shows the job (Slurm COMPLETING /
    squeue lag). The reporter-backed arm settles ``complete`` without waiting
    for the scheduler to purge the record — and stamps the override.

    RED against the pre-fix gate: ``not alive`` blocked the arm, so the run
    stayed ``in_flight`` until a later reconcile found the records purged.
    """
    harvests = _count_harvests(monkeypatch)
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: {"100", "200"})
    _stub_reporter(
        monkeypatch, {"complete": 4, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
    )
    upsert_run(tmp_path, _record("lag_r6", total_tasks=4))

    result = recon.reconcile(tmp_path, "lag_r6", scheduler="sge")

    assert result.status == "complete"
    last = result.last_status or {}
    assert last["verdict_reason"] == "all_tasks_complete"
    # The liveness override is disclosed — the settle is never silent about
    # having outvoted the scheduler's still-live record.
    assert last["scheduler_alive_at_settle"] is True
    # Guaranteed harvest fired once on the in_flight→complete transition.
    assert harvests == ["complete"]


@pytest.mark.parametrize(
    "summary",
    [
        # Retry in flight: two done, two RUNNING — never settle.
        {"complete": 2, "running": 2, "pending": 0, "failed": 0, "unknown": 0},
        # Requeue: a task back in PENDING — never settle.
        {"complete": 3, "running": 0, "pending": 1, "failed": 0, "unknown": 0},
        # A positive failure WHILE ALIVE is not settled ``failed`` either — a
        # requeue may follow; the failure arm keeps its not-alive precondition.
        {"complete": 2, "running": 0, "pending": 0, "failed": 2, "unknown": 0},
        # Stale failed bucket under complete == total: STRICT completion
        # requires every non-complete bucket zero (the lenient-vs-strict
        # divergence, pinned by test_classify) — no settle.
        {"complete": 4, "running": 0, "pending": 0, "failed": 1, "unknown": 0},
        # An unknown bucket is not proven completion — no settle.
        {"complete": 3, "running": 0, "pending": 0, "failed": 0, "unknown": 1},
        # Fewer than total complete, nothing else: not proven — no settle.
        {"complete": 2, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
    ],
    ids=[
        "retry_running",
        "requeue_pending",
        "failed_while_alive",
        "stale_failed_bucket",
        "unknown_bucket",
        "under_total",
    ],
)
def test_non_strict_summary_still_alive_does_not_settle(tmp_path, monkeypatch, summary):
    """The early-settle guard: while the scheduler shows the job, ANY non-strict
    summary (running/pending/failed/unknown > 0, or fewer than total complete)
    keeps the run in_flight — only the fully-proven shape crosses the liveness
    line. Mutation target: a lenient (polling-style) predicate substituted for
    the strict one flips the stale_failed_bucket / failed_while_alive legs.
    """
    harvests = _count_harvests(monkeypatch)
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: {"100", "200"})
    _stub_reporter(monkeypatch, summary)
    upsert_run(tmp_path, _record("live_r7", total_tasks=4))

    result = recon.reconcile(tmp_path, "live_r7", scheduler="sge")

    assert result.status == "in_flight"
    last = result.last_status or {}
    assert last.get("verdict_reason") is None
    assert "scheduler_alive_at_settle" not in last
    assert harvests == []  # no terminal transition


def test_alive_check_failure_blocks_the_strict_override(tmp_path, monkeypatch):
    """The kept gates (1/2): an alive-check SSH failure treats the jobs as
    alive AND sets ``alive_check_failed`` — even a strict all-complete summary
    must not settle (the probe that would prove scheduler-death never ran)."""
    harvests = _count_harvests(monkeypatch)

    def _alive_boom(**_kw):
        raise errors.RemoteCommandFailed("alive check failed (rc=255): connection reset")

    monkeypatch.setattr(recon, "_ssh_alive_job_ids", _alive_boom)
    _stub_reporter(
        monkeypatch, {"complete": 4, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
    )
    upsert_run(tmp_path, _record("probe_r9", total_tasks=4))

    result = recon.reconcile(tmp_path, "probe_r9", scheduler="sge")

    assert result.status == "in_flight"
    last = result.last_status or {}
    assert last.get("verify_state") == "unable_to_verify"
    assert "scheduler_alive_at_settle" not in last
    assert harvests == []


def test_reporter_failure_blocks_the_strict_override(tmp_path, monkeypatch):
    """The kept gates (2/2): a failed reporter walk yields no completion
    evidence at all (``{"error": ...}`` fails the strict predicate by
    construction) and ``reporter_failed`` gates the arm — no settle, routed
    through ``unable_to_verify`` exactly as before."""
    harvests = _count_harvests(monkeypatch)

    def _status_boom(**_kw):
        raise errors.RemoteCommandFailed("status reporter failed (rc=1): boom")

    monkeypatch.setattr(recon, "_ssh_status_report", _status_boom)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: {"100", "200"})
    upsert_run(tmp_path, _record("probe_r10", total_tasks=4))

    result = recon.reconcile(tmp_path, "probe_r10", scheduler="sge")

    assert result.status == "in_flight"
    last = result.last_status or {}
    assert last.get("verify_state") == "unable_to_verify"
    assert "scheduler_alive_at_settle" not in last
    assert harvests == []
