"""Tests for the per-run journal in ``hpc_agent.state.{run_record,journal,index}``."""

from __future__ import annotations

import json
import os
import threading
import warnings
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from hpc_agent.state import index as session_index
from hpc_agent.state import run_record
from hpc_agent.state.index import (
    _all_run_files,
    _index_is_stale,
    _rebuild_index,
    find_in_flight_runs,
    find_runs_by_campaign,
    prune_terminal_runs,
)
from hpc_agent.state.journal import (
    load_run,
    mark_run,
    update_run_status,
    upsert_run,
)
from hpc_agent.state.run_record import (
    RunRecord,
    _atomic_write_json,
    _run_path,
    journal_dir,
    repo_hash,
    runs_dir,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HPC_HOMEDIR into a per-test tmp directory.

    HPC_HOMEDIR lives in :mod:`hpc_agent.state.run_record`
    after the session.py split; patching the module attribute is what
    every reader sees because callers look the name up at call time.
    """
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
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
        "total_tasks": 100,
        "submitted_at": "2026-04-26T17:00:00+00:00",
        "experiment_dir": "/tmp/exp",
    }
    base.update(overrides)
    return RunRecord(**base)


def test_upsert_then_load_roundtrip(journal_home, experiment):
    record = _make_record()
    upsert_run(experiment, record)

    loaded = load_run(experiment, record.run_id)
    assert loaded is not None
    assert loaded.run_id == record.run_id
    assert loaded.profile == "ml_ridge"
    assert loaded.job_ids == ["12345678"]
    assert loaded.combined_waves == []


def test_upsert_idempotent(journal_home, experiment):
    record = _make_record()
    upsert_run(experiment, record)
    upsert_run(experiment, record)

    files = list(runs_dir(experiment).glob("*.json"))
    assert len(files) == 1
    idx = json.loads((journal_dir(experiment) / "index.json").read_text())
    assert list(idx.keys()) == [record.run_id]


def test_update_run_status_partial(journal_home, experiment):
    record = _make_record()
    upsert_run(experiment, record)

    updated = update_run_status(
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
    upsert_run(experiment, _make_record())
    with pytest.raises(ValueError, match="unknown field"):
        update_run_status(experiment, "ridge_abcd1234", profile="hacked")


def test_mark_run_removes_from_in_flight(journal_home, experiment):
    record = _make_record()
    upsert_run(experiment, record)
    assert len(find_in_flight_runs(experiment)) == 1

    mark_run(experiment, record.run_id, status="complete", stage="done")
    assert find_in_flight_runs(experiment) == []


def test_find_in_flight_with_missing_index(journal_home, experiment):
    record = _make_record()
    upsert_run(experiment, record)

    idx_path = journal_dir(experiment) / "index.json"
    idx_path.unlink()
    in_flight = find_in_flight_runs(experiment)
    assert len(in_flight) == 1
    assert in_flight[0].run_id == record.run_id
    assert idx_path.exists()


def test_atomic_write_survives_partial_write(journal_home, experiment):
    record = _make_record()
    upsert_run(experiment, record)

    rdir = runs_dir(experiment)
    (rdir / f"{record.run_id}.json.tmp").write_text("garbage")

    in_flight = find_in_flight_runs(experiment)
    assert len(in_flight) == 1
    assert in_flight[0].run_id == record.run_id


def test_lock_file_skipped_by_loader(journal_home, experiment):
    record = _make_record()
    upsert_run(experiment, record)

    rdir = runs_dir(experiment)
    lock_files = list(rdir.glob("*.lock"))
    assert lock_files
    in_flight = find_in_flight_runs(experiment)
    assert len(in_flight) == 1
    assert all(r.run_id == record.run_id for r in in_flight)


def test_prune_keeps_in_flight(journal_home, experiment):
    in_flight_record = _make_record(run_id="active_aaaa1111")
    upsert_run(experiment, in_flight_record)

    for i in range(5):
        rid = f"done_{i:08d}"
        upsert_run(experiment, _make_record(run_id=rid))
        mark_run(experiment, rid, status="complete", stage="done")

    removed = prune_terminal_runs(experiment, keep=2)
    assert removed == 3

    files = {p.stem for p in runs_dir(experiment).glob("*.json")}
    assert "active_aaaa1111" in files
    terminal_remaining = files - {"active_aaaa1111"}
    assert len(terminal_remaining) == 2


def test_no_journal_dir_returns_none(journal_home, experiment):
    assert find_in_flight_runs(experiment) == []
    assert load_run(experiment, "nonexistent") is None


def test_repo_hash_normalizes_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert repo_hash(real) == repo_hash(link)


def test_schema_version_mismatch_skipped(journal_home, experiment):
    record = _make_record()
    upsert_run(experiment, record)

    path = runs_dir(experiment) / f"{record.run_id}.json"
    payload = json.loads(path.read_text())
    payload["schema_version"] = 999
    path.write_text(json.dumps(payload))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = load_run(experiment, record.run_id)
    assert loaded is None
    assert any("schema_version" in str(w.message) for w in caught)


def test_concurrent_writers_serialize(journal_home, experiment):
    """Two threads updating distinct fields end with both writes applied."""
    record = _make_record()
    upsert_run(experiment, record)

    def writer(field: str, value):
        for _ in range(20):
            update_run_status(experiment, record.run_id, **{field: value})

    t1 = threading.Thread(target=writer, args=("combined_waves", [0, 1]))
    t2 = threading.Thread(
        target=writer,
        args=("retries", {"7": {"attempts": 1, "category": "system_oom", "overrides": {}}}),
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    final = load_run(experiment, record.run_id)
    assert final is not None
    assert final.combined_waves == [0, 1]
    assert final.retries == {"7": {"attempts": 1, "category": "system_oom", "overrides": {}}}


def test_repo_meta_records_experiment_dir(journal_home, experiment):
    upsert_run(experiment, _make_record())
    repo_meta = json.loads((journal_dir(experiment) / "repo.json").read_text())
    assert repo_meta["experiment_dir"] == str(experiment.resolve())
    assert "first_seen" in repo_meta


# ─── Bug 10: prune updates the index in one batch, not per-deletion ────────


def test_prune_terminal_runs_writes_index_once(journal_home, experiment):
    """Previously the index was rewritten inside the per-run deletion loop:
    pruning N runs cost N atomic writes + N flocks and left a window where
    deleted runs still appeared in the index.  The fix collects deletions
    and updates once at the end.
    """
    # Seed 5 terminal runs and 1 in-flight run.
    for i in range(5):
        rec = _make_record(run_id=f"r_terminal_{i:08x}", status="complete")
        upsert_run(experiment, rec)
    upsert_run(experiment, _make_record(run_id="r_inflight"))

    # Force the staleness check to find the index needs rebuilding once
    # before prune_terminal_runs touches it.
    _ = _index_is_stale(experiment)

    real_atomic = _atomic_write_json
    idx_path = journal_dir(experiment) / "index.json"
    calls: list[Path] = []

    def tracking(path, payload):
        if path == idx_path:
            calls.append(path)
        return real_atomic(path, payload)

    # ``_atomic_write_json`` lives in run_record but ``prune_terminal_runs``
    # is in :mod:`hpc_agent.state.index`; patching at the call-site module is what
    # the resolver actually sees (the import at module load time bound the
    # name into ``index``'s namespace).
    with patch.object(session_index, "_atomic_write_json", side_effect=tracking):
        removed = prune_terminal_runs(experiment, keep=1)

    assert removed == 4
    # One write to the index, regardless of how many runs were pruned.
    assert len(calls) == 1


def test_prune_terminal_runs_unlinks_last_status_cache(journal_home, experiment):
    """The per-run last_status.json cache file lives in the same runs/
    directory; pruning a terminal run should reap the cache too so it
    doesn't accumulate forever.
    """
    rec = _make_record(run_id="r_terminal_abcdef00", status="complete")
    upsert_run(experiment, rec)
    cache = runs_dir(experiment) / f"{rec.run_id}.last_status.json"
    cache.write_text(json.dumps({"complete": 0}))

    prune_terminal_runs(experiment, keep=0)

    assert not cache.exists()


# ─── Bug 14: last_status.json files don't trigger false index rebuilds ────


def test_last_status_files_excluded_from_staleness_check(journal_home, experiment):
    """A status poll writes ``runs/<run_id>.last_status.json`` next to the
    journal record.  Including those in ``_all_run_files`` would force an
    index rebuild on every poll because their mtime advances each time.
    """
    rec = _make_record()
    upsert_run(experiment, rec)

    # Force a fresh index so we're starting from "not stale".
    _rebuild_index(experiment)
    assert _index_is_stale(experiment) is False

    # Touch the cache file to advance its mtime.
    cache = runs_dir(experiment) / f"{rec.run_id}.last_status.json"
    cache.write_text("{}")
    # Bump mtime past the index by a comfortable margin.
    idx_path = journal_dir(experiment) / "index.json"
    future = idx_path.stat().st_mtime + 5
    os.utime(cache, (future, future))

    # Cache file is *not* a journal record — staleness should not flip.
    assert _index_is_stale(experiment) is False


def test_last_status_files_not_returned_by_all_run_files(journal_home, experiment):
    """Direct test of the filter — if this regresses, prune and rebuild
    will start treating the cache as a record.
    """
    rec = _make_record()
    upsert_run(experiment, rec)
    cache = runs_dir(experiment) / f"{rec.run_id}.last_status.json"
    cache.write_text("{}")

    files = _all_run_files(experiment)
    assert cache not in files
    assert any(p.name == f"{rec.run_id}.json" for p in files)


# ---------------------------------------------------------------------------
# Closed-loop campaigns: campaign_id field + find_runs_by_campaign
# ---------------------------------------------------------------------------


def test_campaign_id_default_is_empty_string(journal_home, experiment):
    """Open-loop submits leave campaign_id empty so they don't get matched
    by find_runs_by_campaign."""
    rec = _make_record()
    assert rec.campaign_id == ""
    upsert_run(experiment, rec)
    loaded = load_run(experiment, rec.run_id)
    assert loaded is not None
    assert loaded.campaign_id == ""


def test_campaign_id_round_trips_through_journal(journal_home, experiment):
    rec = _make_record(campaign_id="ml_ridge_q1")
    upsert_run(experiment, rec)
    loaded = load_run(experiment, rec.run_id)
    assert loaded is not None
    assert loaded.campaign_id == "ml_ridge_q1"


def test_find_runs_by_campaign_filters_and_orders_oldest_first(journal_home, experiment):
    upsert_run(experiment, _make_record(run_id="r1", campaign_id="A"))
    upsert_run(experiment, _make_record(run_id="r2", campaign_id="B"))
    upsert_run(experiment, _make_record(run_id="r3", campaign_id="A"))
    # Pin distinct, ascending mtimes — run_ids ``r1``/``r2``/``r3`` are not
    # ISO-sortable, so the oldest-first ordering must come from mtime, not
    # the filename.
    t0 = 1_700_000_000.0
    for i, run_id in enumerate(("r1", "r2", "r3")):
        os.utime(_run_path(experiment, run_id), (t0 + i, t0 + i))

    matched = find_runs_by_campaign(experiment, "A")
    assert [r.run_id for r in matched] == ["r1", "r3"]


def test_find_runs_by_campaign_empty_id_returns_empty(journal_home, experiment):
    upsert_run(experiment, _make_record(campaign_id="A"))
    assert find_runs_by_campaign(experiment, "") == []


def test_find_runs_by_campaign_unknown_id_returns_empty(journal_home, experiment):
    upsert_run(experiment, _make_record(campaign_id="A"))
    assert find_runs_by_campaign(experiment, "B") == []
