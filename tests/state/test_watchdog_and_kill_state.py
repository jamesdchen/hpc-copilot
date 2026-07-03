"""Tests for the §5 driver-watchdog + kill durable state.

Covers the new RunRecord fields (round-trip), the journal setters
(``stamp_tick`` / ``mark_seen_by_human`` / ``record_kill_request`` /
``record_kill_confirmed``), and ``find_stalled_runs``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.state.index import find_stalled_runs
from hpc_agent.state.journal import (
    load_run,
    mark_seen_by_human,
    record_kill_confirmed,
    record_kill_request,
    stamp_tick,
    upsert_run,
)
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(
    run_id: str, *, status: str = "in_flight", job_ids: list[str] | None = None
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=job_ids if job_ids is not None else ["100"],
        total_tasks=4,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status=status,
    )


# ── RunRecord field round-trip ────────────────────────────────────────────────


def test_new_fields_default_and_roundtrip(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1"))
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    # Harmless defaults on a freshly-written record.
    assert rec.last_tick_at is None
    assert rec.next_tick_due is None
    assert rec.last_seen_by_human_at is None
    assert rec.kill_requested_at is None
    assert rec.kill_confirmed_at is None
    assert rec.kill_requested_job_ids == []
    assert rec.kill_confirmed_job_ids == []

    # A record carrying set values survives a to_dict/from_dict round-trip.
    populated = _record("r2")
    populated.last_tick_at = "2026-07-03T01:00:00+00:00"
    populated.next_tick_due = "2026-07-03T01:05:00+00:00"
    populated.last_seen_by_human_at = "2026-07-03T00:59:00+00:00"
    populated.kill_requested_at = "2026-07-03T02:00:00+00:00"
    populated.kill_confirmed_at = "2026-07-03T02:00:30+00:00"
    populated.kill_requested_job_ids = ["100", "200"]
    populated.kill_confirmed_job_ids = ["100"]
    assert RunRecord.from_dict(populated.to_dict()) == populated


# ── journal setters ───────────────────────────────────────────────────────────


def test_stamp_tick_sets_deadline_fields(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1"))
    stamp_tick(
        "r1",
        last_tick_at="2026-07-03T01:00:00+00:00",
        next_tick_due="2026-07-03T01:05:00+00:00",
        experiment_dir=tmp_path,
    )
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    assert rec.last_tick_at == "2026-07-03T01:00:00+00:00"
    assert rec.next_tick_due == "2026-07-03T01:05:00+00:00"


def test_stamp_tick_defaults_experiment_dir_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned cross-unit form ``stamp_tick(run_id, ...)`` (no experiment_dir)
    resolves the journal home from the current working directory."""
    monkeypatch.chdir(tmp_path)
    upsert_run(Path.cwd(), _record("r1"))
    stamp_tick(
        "r1",
        last_tick_at="2026-07-03T01:00:00+00:00",
        next_tick_due="2026-07-03T01:05:00+00:00",
    )
    rec = load_run(Path.cwd(), "r1")
    assert rec is not None
    assert rec.next_tick_due == "2026-07-03T01:05:00+00:00"


def test_mark_seen_by_human_sets_marker(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1"))
    mark_seen_by_human("r1", at="2026-07-03T03:00:00+00:00", experiment_dir=tmp_path)
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    assert rec.last_seen_by_human_at == "2026-07-03T03:00:00+00:00"


def test_record_kill_request_and_confirmed(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1", job_ids=["100", "200", "300"]))
    record_kill_request(
        "r1",
        requested_at="2026-07-03T04:00:00+00:00",
        job_ids=["100", "200", "300"],
        experiment_dir=tmp_path,
    )
    record_kill_confirmed(
        "r1",
        confirmed_at="2026-07-03T04:00:30+00:00",
        job_ids=["100", "300"],
        experiment_dir=tmp_path,
    )
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    assert rec.kill_requested_at == "2026-07-03T04:00:00+00:00"
    assert rec.kill_requested_job_ids == ["100", "200", "300"]
    assert rec.kill_confirmed_at == "2026-07-03T04:00:30+00:00"
    assert rec.kill_confirmed_job_ids == ["100", "300"]


def test_setter_raises_on_missing_record(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        stamp_tick(
            "nope",
            last_tick_at="2026-07-03T01:00:00+00:00",
            next_tick_due="2026-07-03T01:05:00+00:00",
            experiment_dir=tmp_path,
        )


# ── find_stalled_runs ─────────────────────────────────────────────────────────


def test_find_stalled_runs_only_flags_past_deadline_live_runs(tmp_path: Path) -> None:
    now = "2026-07-03T01:00:00+00:00"
    # stalled: deadline in the past
    upsert_run(tmp_path, _record("stalled"))
    stamp_tick(
        "stalled",
        last_tick_at="2026-07-02T23:55:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    # healthy: deadline in the future
    upsert_run(tmp_path, _record("healthy"))
    stamp_tick(
        "healthy",
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",
        experiment_dir=tmp_path,
    )
    # unticked: no deadline stamped → not a miss
    upsert_run(tmp_path, _record("unticked"))
    # terminal: past deadline but not in_flight → excluded
    upsert_run(tmp_path, _record("done", status="complete"))
    stamp_tick(
        "done",
        last_tick_at="2026-07-02T23:00:00+00:00",
        next_tick_due="2026-07-02T23:30:00+00:00",
        experiment_dir=tmp_path,
    )

    stalled = find_stalled_runs(now, experiment_dir=tmp_path)
    assert [s["run_id"] for s in stalled] == ["stalled"]
    hit = stalled[0]
    assert hit["status"] == "in_flight"
    assert hit["next_tick_due"] == "2026-07-03T00:00:00+00:00"
    assert hit["last_tick_at"] == "2026-07-02T23:55:00+00:00"
    assert hit["cluster"] == "c"
    assert hit["ssh_target"] == "u@h"


def test_find_stalled_runs_rejects_malformed_now(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        find_stalled_runs("not-a-timestamp", experiment_dir=tmp_path)
