"""Tests for the ``doctor`` driver-watchdog query (§5).

Detection only: doctor surfaces stalled runs as drafted proposals and never
restarts or re-arms anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent.ops.recover.doctor import doctor
from hpc_agent.state.journal import stamp_tick, upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, status: str = "in_flight") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status=status,
    )


def test_doctor_surfaces_only_the_stalled_run(tmp_path: Path) -> None:
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("stalled"))
    stamp_tick(
        "stalled",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    upsert_run(tmp_path, _record("healthy"))
    stamp_tick(
        "healthy",
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",
        experiment_dir=tmp_path,
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))
    assert out["now"] == now
    assert out["stalled_count"] == 1
    hit = out["stalled"][0]
    assert hit["run_id"] == "stalled"
    assert hit["status"] == "in_flight"
    assert hit["cluster"] == "hoffman2"
    assert hit["ssh_target"] == "u@h"
    # Drafted proposal + evidence, never an action.
    assert "stalled" in hit["proposal"].lower()
    assert "re-arm" in hit["proposal"].lower()
    assert hit["evidence"]["overdue_seconds"] == 3600
    assert hit["evidence"]["now"] == now


def test_doctor_empty_when_nothing_overdue(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("healthy"))
    stamp_tick(
        "healthy",
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",
        experiment_dir=tmp_path,
    )
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["stalled_count"] == 0
    assert out["stalled"] == []


def test_doctor_rejects_malformed_now(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="not-a-timestamp"))
