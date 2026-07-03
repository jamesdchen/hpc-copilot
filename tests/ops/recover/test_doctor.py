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
from hpc_agent.state.journal import mark_pending_decision, stamp_tick, upsert_run
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


# ─── parked ≠ stalled (§5) ──────────────────────────────────────────────────


def _park(exp: Path, run_id: str) -> None:
    mark_pending_decision(
        run_id,
        block="s2",
        workflow="submit",
        brief={"proposal": "greenlight the canary?"},
        resume_cursor={"workflow": "submit", "run_id": run_id, "next_verb": "s3"},
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=exp,
    )


def test_doctor_reports_parked_run_not_stalled(tmp_path: Path) -> None:
    """A run past its tick deadline BUT carrying a pending_decision marker is
    parked (awaiting the human), never stalled — the §5 "parked ≠ stalled" read."""
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("parked"))
    stamp_tick(
        "parked",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",  # overdue: would be stalled...
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "parked")  # ...but the marker flips the read

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    # Never in the stalled list.
    assert out["stalled_count"] == 0
    assert out["stalled"] == []
    assert all(p["run_id"] != "parked" for p in out["stalled"])
    # Surfaced in parked with the awaiting read, not a re-arm proposal.
    assert out["parked_count"] == 1
    note = out["parked"][0]
    assert note["run_id"] == "parked"
    assert note["block"] == "s2"
    assert note["workflow"] == "submit"
    assert note["awaiting_since"] == "2026-07-03T00:30:00+00:00"
    assert "awaiting your decision" in note["note"].lower()
    assert "re-arm" not in note["note"].lower()


def test_doctor_separates_parked_from_stalled(tmp_path: Path) -> None:
    now = "2026-07-03T01:00:00+00:00"
    # A genuinely stalled run (overdue, no marker).
    upsert_run(tmp_path, _record("stalled"))
    stamp_tick(
        "stalled",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    # A parked run (overdue but awaiting a decision).
    upsert_run(tmp_path, _record("parked"))
    stamp_tick(
        "parked",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "parked")

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    assert [p["run_id"] for p in out["stalled"]] == ["stalled"]
    assert [p["run_id"] for p in out["parked"]] == ["parked"]


def test_doctor_no_parked_when_none_awaiting(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("healthy"))
    stamp_tick(
        "healthy",
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",
        experiment_dir=tmp_path,
    )
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["parked_count"] == 0
    assert out["parked"] == []
