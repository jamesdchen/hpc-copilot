"""Render tolerance for a ``submitting`` record in the status paths (submit-once
U3, phase 1). A journal carrying a submitting record must render — never crash —
and display its phase honestly ("submitting — dispatch in flight / awaiting id",
submit-once design §3.3 / premortem Δ8). No schema value, no lifecycle_state
echo: the display projection lives only in the snapshot/doctor renders.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec
from hpc_agent.ops.relay_render import _render_snapshot, _row_line
from hpc_agent.ops.status_blocks import ANOMALY_STATUSES, digest_run, status_snapshot
from hpc_agent.state.journal import stamp_tick, upsert_run
from hpc_agent.state.run_record import RunRecord

_NOW = "2026-07-17T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _mk(exp: Path, run_id: str, *, status: str = "submitting") -> RunRecord:
    rec = RunRecord(
        run_id=run_id,
        profile="prof",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path="/scratch/run",
        job_name="job",
        job_ids=[] if status == "submitting" else ["1"],
        total_tasks=10,
        submitted_at="2026-07-17T00:00:00+00:00",
        experiment_dir=str(exp),
        status=status,
    )
    upsert_run(exp, rec)
    return rec


def test_row_line_displays_the_submitting_phase() -> None:
    row = digest_run(_row_source(status="submitting"))
    line = _row_line(row)
    assert "submitting — dispatch in flight / awaiting id" in line
    assert "on hoffman2" in line


def test_submitting_is_not_an_anomaly_status() -> None:
    """A submitting record is NOT failed/abandoned — the snapshot must not flag
    it as an anomaly needing a recovery decision."""
    assert "submitting" not in ANOMALY_STATUSES


def test_render_snapshot_tolerates_a_submitting_row() -> None:
    brief = {
        "running_where": [digest_run(_row_source(status="submitting"))],
        "anomalies": [],
        "stalled_runs": [],
    }
    rendered = _render_snapshot(brief)
    assert "submitting — dispatch in flight" in rendered


def test_status_snapshot_renders_a_submitting_record_without_crashing(tmp_path: Path) -> None:
    """End-to-end: a snapshot querying a submitting run by id digests and renders
    it cleanly (the load_run path), is not an anomaly, and needs no decision
    (readers-only phase). The bare-fleet gather stays ``find_in_flight_runs``
    (unchanged per design — submitting is not the monitor live set), so the
    per-run query is the render surface a human hits for a submitting record."""
    _mk(tmp_path, "sub-orphan", status="submitting")
    result = status_snapshot(
        tmp_path, spec=StatusSnapshotSpec(run_id="sub-orphan", now_iso=_NOW, mark_seen=False)
    )
    assert result.needs_decision is False
    rows = result.brief["running_where"]
    assert [r["run_id"] for r in rows] == ["sub-orphan"]
    assert rows[0]["status"] == "submitting"
    assert result.brief["anomalies"] == []
    assert "submitting — dispatch in flight / awaiting id" in result.relay


def test_status_snapshot_surfaces_a_stalled_submitting_run(tmp_path: Path) -> None:
    """A submitting record whose watchdog stamp lapsed surfaces as a stalled
    hit → the snapshot needs a decision (routes to reconcile-recovery)."""
    _mk(tmp_path, "stuck", status="submitting")
    stamp_tick(
        "stuck",
        last_tick_at="2026-07-17T11:55:00+00:00",
        next_tick_due="2026-07-17T11:59:00+00:00",  # past _NOW
        experiment_dir=tmp_path,
    )
    result = status_snapshot(tmp_path, spec=StatusSnapshotSpec(now_iso=_NOW, mark_seen=False))
    assert result.needs_decision is True
    assert [s["run_id"] for s in result.brief["stalled_runs"]] == ["stuck"]
    assert result.brief["stalled_runs"][0]["status"] == "submitting"


def _row_source(*, status: str) -> RunRecord:
    return RunRecord(
        run_id="r1",
        profile="prof",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path="/scratch/run",
        job_name="job",
        job_ids=[] if status == "submitting" else ["1"],
        total_tasks=10,
        submitted_at="2026-07-17T00:00:00+00:00",
        experiment_dir="/exp",
        status=status,
    )
