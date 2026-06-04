"""Tests for the pending-verdict holding state (#231/#234).

A run parked on an escalation it cannot deterministically resolve enters a
*held* state: ``pending_verdict`` carries the escalation block, the run is
neither in-flight nor terminal-done from the campaign loop's view, and the
loop keeps progressing on unaffected work until a verdict is applied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.state.index import find_held_runs, find_in_flight_runs
from hpc_agent.state.journal import (
    clear_pending_verdict,
    is_held,
    load_run,
    mark_pending_verdict,
    mark_run,
    upsert_run,
)
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, campaign_id: str = "", status: str = "in_flight") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["1"],
        total_tasks=4,
        submitted_at="2026-06-04T00:00:00Z",
        experiment_dir="/exp",
        status=status,
        campaign_id=campaign_id,
    )


_ESCALATION = {
    "decided_by": "judgement",
    "reason": "novel OOM signature",
    "failure_features": {"error_class": "unknown"},
    "cluster": {"fingerprint": "fp1", "task_ids": ["t1", "t2"]},
}


def test_fresh_record_is_not_held(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1"))
    assert is_held(load_run(tmp_path, "r1")) is False
    assert find_held_runs(tmp_path) == []


def test_mark_parks_the_run_with_the_escalation(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1", status="failed"))
    mark_pending_verdict(tmp_path, "r1", escalation=_ESCALATION)

    rec = load_run(tmp_path, "r1")
    assert is_held(rec) is True
    # The escalation block round-trips verbatim (stored as a plain dict).
    assert rec.pending_verdict["cluster"]["task_ids"] == ["t1", "t2"]
    held = find_held_runs(tmp_path)
    assert [r.run_id for r in held] == ["r1"]


def test_held_run_does_not_block_as_in_flight(tmp_path: Path) -> None:
    """A failed-but-held run is parked, NOT live — so the campaign loop is
    not blocked waiting on it as in-flight work."""
    upsert_run(tmp_path, _record("held", status="failed"))
    upsert_run(tmp_path, _record("live", status="in_flight"))
    mark_pending_verdict(tmp_path, "held", escalation=_ESCALATION)

    in_flight_ids = {r.run_id for r in find_in_flight_runs(tmp_path)}
    assert in_flight_ids == {"live"}  # held run is not surfaced as live
    held_ids = {r.run_id for r in find_held_runs(tmp_path)}
    assert held_ids == {"held"}


def test_clear_releases_the_hold(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1", status="failed"))
    mark_pending_verdict(tmp_path, "r1", escalation=_ESCALATION)
    clear_pending_verdict(tmp_path, "r1")

    assert is_held(load_run(tmp_path, "r1")) is False
    assert find_held_runs(tmp_path) == []


def test_clear_is_idempotent_on_unheld_run(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1"))
    clear_pending_verdict(tmp_path, "r1")  # no-op, must not raise
    assert is_held(load_run(tmp_path, "r1")) is False


def test_mark_rejects_empty_escalation(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1"))
    with pytest.raises(ValueError):
        mark_pending_verdict(tmp_path, "r1", escalation={})


def test_find_held_runs_narrows_by_campaign(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("a", campaign_id="camp1", status="failed"))
    upsert_run(tmp_path, _record("b", campaign_id="camp2", status="failed"))
    mark_pending_verdict(tmp_path, "a", escalation=_ESCALATION)
    mark_pending_verdict(tmp_path, "b", escalation=_ESCALATION)

    assert {r.run_id for r in find_held_runs(tmp_path, campaign_id="camp1")} == {"a"}
    assert {r.run_id for r in find_held_runs(tmp_path)} == {"a", "b"}


def test_pending_verdict_survives_status_transition(tmp_path: Path) -> None:
    """A verdict can be pending across a terminal status mark — the hold is
    orthogonal to status (a live-running task runs to terminal, then holds)."""
    upsert_run(tmp_path, _record("r1", status="in_flight"))
    mark_pending_verdict(tmp_path, "r1", escalation=_ESCALATION)
    mark_run(tmp_path, "r1", status="failed")
    assert is_held(load_run(tmp_path, "r1")) is True
