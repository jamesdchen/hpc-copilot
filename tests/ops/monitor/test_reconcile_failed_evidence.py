"""reconcile's FAILED-verdict evidence quotes an actually-FAILED task's stderr.

``_gather_failure_features`` used to fetch ``task_ids=[0]`` unconditionally, so
a multi-task run whose task 0 SUCCEEDED while another task failed attached the
successful task's stderr as the run's failure evidence — correct only for
1-task canaries. The reporter's per-task statuses (``report["tasks"]``, keyed
by 0-based ``HPC_TASK_ID``) are now consulted: the first (lowest-id) failed
task's log is fetched, falling back to task 0 only when the report carries no
per-task ``failed`` entry.

Cluster-free: the three SSH fan-out calls and the lazily-imported log fetch are
monkeypatched; the assertion is on WHICH task ids reach the fetch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.ops.monitor import reconcile as recon
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, total_tasks: int) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["13548839"],
        total_tasks=total_tasks,
        submitted_at="2026-07-03T00:00:00Z",
        experiment_dir="/exp",
        status="in_flight",
    )


def _stub_cluster(monkeypatch: pytest.MonkeyPatch, *, report: dict) -> None:
    """Stub the three SSH calls reconcile fans out (nothing alive, no waves)."""
    monkeypatch.setattr(recon, "_ssh_status_report", lambda **_kw: report)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: set())


def _capture_log_fetch(monkeypatch: pytest.MonkeyPatch, *, tail: str) -> list[list[int]]:
    """Capture the ``task_ids`` each (lazily imported) log fetch is asked for."""
    fetched: list[list[int]] = []

    def _fake(*, task_ids: list[int], **_kw) -> list[dict]:  # type: ignore[no-untyped-def]
        fetched.append(list(task_ids))
        return [
            {"task_id": tid, "content": tail, "path": f"/remote/logs/j.o13548839.{tid + 1}"}
            for tid in task_ids
        ]

    monkeypatch.setattr("hpc_agent.infra.cluster_logs.fetch_task_logs", _fake)
    return fetched


def test_failure_evidence_comes_from_the_failed_task_not_task_zero(tmp_path, monkeypatch):
    """Task 0 complete + task 7 failed → the evidence log fetch targets task 7."""
    upsert_run(tmp_path, _record("multi_fail", total_tasks=8))
    tasks = {str(t): {"status": "complete"} for t in range(7)}
    tasks["7"] = {"status": "failed", "exit_code": 1}
    _stub_cluster(
        monkeypatch,
        report={
            "summary": {"complete": 7, "running": 0, "pending": 0, "failed": 1, "unknown": 0},
            "tasks": tasks,
            "waves": {},
        },
    )
    fetched = _capture_log_fetch(monkeypatch, tail="RuntimeError: boom in task 7")

    result = recon.reconcile(tmp_path, "multi_fail", scheduler="sge")

    assert result.status == "failed"
    # The regression target: the fetch asked for the FAILED task's log, not [0].
    assert fetched == [[7]]
    features = (result.last_status or {}).get("failure_features")
    assert isinstance(features, dict)
    assert features["cluster_log_tail"] == "RuntimeError: boom in task 7"
    assert features["log_path"] == "/remote/logs/j.o13548839.8"


def test_multiple_failed_tasks_fetch_the_lowest_id(tmp_path, monkeypatch):
    """Bounded evidence: several failures still cost ONE fetch — the first
    (lowest-id) failed task, mirroring the one-tail shape verify_canary
    attaches."""
    upsert_run(tmp_path, _record("multi_fail2", total_tasks=6))
    tasks = {
        "0": {"status": "complete"},
        "1": {"status": "complete"},
        "2": {"status": "failed"},
        "3": {"status": "complete"},
        "4": {"status": "failed"},
        "5": {"status": "failed"},
    }
    _stub_cluster(
        monkeypatch,
        report={
            "summary": {"complete": 3, "running": 0, "pending": 0, "failed": 3, "unknown": 0},
            "tasks": tasks,
            "waves": {},
        },
    )
    fetched = _capture_log_fetch(monkeypatch, tail="boom")

    result = recon.reconcile(tmp_path, "multi_fail2", scheduler="sge")

    assert result.status == "failed"
    assert fetched == [[2]]


def test_report_without_per_task_statuses_falls_back_to_task_zero(tmp_path, monkeypatch):
    """A summary-only report (no ``tasks`` map) keeps the pre-fix behavior:
    the verdict stands on the counts and the fetch degrades to task 0."""
    upsert_run(tmp_path, _record("counts_only", total_tasks=4))
    _stub_cluster(
        monkeypatch,
        report={
            "summary": {"complete": 3, "running": 0, "pending": 0, "failed": 1, "unknown": 0},
            "waves": {},
        },
    )
    fetched = _capture_log_fetch(monkeypatch, tail="boom")

    result = recon.reconcile(tmp_path, "counts_only", scheduler="sge")

    assert result.status == "failed"
    assert fetched == [[0]]


def test_failed_evidence_task_ids_selector():
    """The selector itself: failed statuses win, lowest id, [0] fallback,
    malformed entries skipped."""
    assert recon._failed_evidence_task_ids({}) == [0]
    assert recon._failed_evidence_task_ids({"tasks": {}}) == [0]
    assert recon._failed_evidence_task_ids({"tasks": {"0": {"status": "complete"}}}) == [0]
    assert recon._failed_evidence_task_ids(
        {"tasks": {"0": {"status": "complete"}, "7": {"status": "failed"}}}
    ) == [7]
    assert recon._failed_evidence_task_ids(
        {"tasks": {"9": {"status": "failed"}, "4": {"status": "failed"}}}
    ) == [4]
    # Malformed keys/entries never raise — they are simply not evidence.
    assert recon._failed_evidence_task_ids(
        {"tasks": {"x": {"status": "failed"}, "3": "failed", "5": {"status": "failed"}}}
    ) == [5]
