"""Tests for the per-run journal in ``slash_commands.session``."""

from __future__ import annotations

import json
import os
import threading
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from slash_commands import session
from slash_commands.session import RunRecord


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HPC_HOMEDIR into a per-test tmp directory."""
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(session, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    """A throwaway experiment dir on disk."""
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _make_record(run_id: str = "ridge_abcd1234", **overrides) -> RunRecord:
    base = {
        "run_id": run_id,
        "profile": "ml_ridge",
        "cluster": "hoffman2",
        "ssh_target": "user@hoffman2.idre.ucla.edu",
        "remote_path": "/u/scratch/exp",
        "job_name": "ml_ridge",
        "job_ids": ["12345678"],
        "manifest": "manifest.abcd1234.json",
        "total_tasks": 100,
        "submitted_at": "2026-04-26T17:00:00+00:00",
        "experiment_dir": "/tmp/exp",
    }
    base.update(overrides)
    return RunRecord(**base)


def test_upsert_then_load_roundtrip(journal_home, experiment):
    record = _make_record()
    session.upsert_run(experiment, record)

    loaded = session.load_run(experiment, record.run_id)
    assert loaded is not None
    assert loaded.run_id == record.run_id
    assert loaded.profile == "ml_ridge"
    assert loaded.job_ids == ["12345678"]
    assert loaded.combined_waves == []


def test_upsert_idempotent(journal_home, experiment):
    record = _make_record()
    session.upsert_run(experiment, record)
    session.upsert_run(experiment, record)

    files = list(session.runs_dir(experiment).glob("*.json"))
    assert len(files) == 1
    idx = json.loads((session.journal_dir(experiment) / "index.json").read_text())
    assert list(idx.keys()) == [record.run_id]


def test_update_run_status_partial(journal_home, experiment):
    record = _make_record()
    session.upsert_run(experiment, record)

    updated = session.update_run_status(
        experiment,
        record.run_id,
        last_status={"complete": 50, "running": 30, "failed": 0, "checked_at": "now"},
        combined_waves=[0, 1],
    )
    assert updated.last_status["complete"] == 50
    assert updated.combined_waves == [0, 1]
    assert updated.profile == "ml_ridge"
    assert updated.cluster == "hoffman2"


def test_update_run_status_rejects_unknown_field(journal_home, experiment):
    session.upsert_run(experiment, _make_record())
    with pytest.raises(ValueError, match="unknown field"):
        session.update_run_status(experiment, "ridge_abcd1234", profile="hacked")


def test_mark_run_removes_from_in_flight(journal_home, experiment):
    record = _make_record()
    session.upsert_run(experiment, record)
    assert len(session.find_in_flight_runs(experiment)) == 1

    session.mark_run(experiment, record.run_id, status="complete", stage="done")
    assert session.find_in_flight_runs(experiment) == []


def test_find_in_flight_with_missing_index(journal_home, experiment):
    record = _make_record()
    session.upsert_run(experiment, record)

    idx_path = session.journal_dir(experiment) / "index.json"
    idx_path.unlink()
    in_flight = session.find_in_flight_runs(experiment)
    assert len(in_flight) == 1
    assert in_flight[0].run_id == record.run_id
    assert idx_path.exists()


def test_atomic_write_survives_partial_write(journal_home, experiment):
    record = _make_record()
    session.upsert_run(experiment, record)

    rdir = session.runs_dir(experiment)
    (rdir / f"{record.run_id}.json.tmp").write_text("garbage")

    in_flight = session.find_in_flight_runs(experiment)
    assert len(in_flight) == 1
    assert in_flight[0].run_id == record.run_id


def test_lock_file_skipped_by_loader(journal_home, experiment):
    record = _make_record()
    session.upsert_run(experiment, record)

    rdir = session.runs_dir(experiment)
    lock_files = list(rdir.glob("*.lock"))
    assert lock_files
    in_flight = session.find_in_flight_runs(experiment)
    assert len(in_flight) == 1
    assert all(r.run_id == record.run_id for r in in_flight)


def test_prune_keeps_in_flight(journal_home, experiment):
    in_flight_record = _make_record(run_id="active_aaaa1111")
    session.upsert_run(experiment, in_flight_record)

    for i in range(5):
        rid = f"done_{i:08d}"
        session.upsert_run(experiment, _make_record(run_id=rid))
        session.mark_run(experiment, rid, status="complete", stage="done")

    removed = session.prune_terminal_runs(experiment, keep=2)
    assert removed == 3

    files = {p.stem for p in session.runs_dir(experiment).glob("*.json")}
    assert "active_aaaa1111" in files
    terminal_remaining = files - {"active_aaaa1111"}
    assert len(terminal_remaining) == 2


def test_no_journal_dir_returns_none(journal_home, experiment):
    assert session.find_in_flight_runs(experiment) == []
    assert session.load_run(experiment, "nonexistent") is None


def test_repo_hash_normalizes_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert session.repo_hash(real) == session.repo_hash(link)


def test_schema_version_mismatch_skipped(journal_home, experiment):
    record = _make_record()
    session.upsert_run(experiment, record)

    path = session.runs_dir(experiment) / f"{record.run_id}.json"
    payload = json.loads(path.read_text())
    payload["schema_version"] = 999
    path.write_text(json.dumps(payload))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = session.load_run(experiment, record.run_id)
    assert loaded is None
    assert any("schema_version" in str(w.message) for w in caught)


def test_concurrent_writers_serialize(journal_home, experiment):
    """Two threads updating distinct fields end with both writes applied."""
    record = _make_record()
    session.upsert_run(experiment, record)

    def writer(field: str, value):
        for _ in range(20):
            session.update_run_status(experiment, record.run_id, **{field: value})

    t1 = threading.Thread(target=writer, args=("combined_waves", [0, 1]))
    t2 = threading.Thread(target=writer, args=("retries", {"7": {"attempts": 1, "category": "system_oom", "overrides": {}}}))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    final = session.load_run(experiment, record.run_id)
    assert final is not None
    assert final.combined_waves == [0, 1]
    assert final.retries == {"7": {"attempts": 1, "category": "system_oom", "overrides": {}}}


def test_repo_meta_records_experiment_dir(journal_home, experiment):
    session.upsert_run(experiment, _make_record())
    repo_meta = json.loads((session.journal_dir(experiment) / "repo.json").read_text())
    assert repo_meta["experiment_dir"] == str(experiment.resolve())
    assert "first_seen" in repo_meta
