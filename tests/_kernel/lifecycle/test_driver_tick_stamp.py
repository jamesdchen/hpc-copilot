"""Watchdog-stamp discipline across a fresh run's first driver span (run-14).

The §5 driver dead-man's-switch (:func:`drive._stamp_driver_tick`) stamps
``last_tick_at`` / ``next_tick_due`` on the JOURNAL record after every span so
the ``doctor`` watchdog can detect a stalled driver. But the journal RunRecord
for a fresh run is minted only INSIDE the (gated, often detached) submit-s2
qsub — S1's resolve leg writes only the per-run sidecar. So the FIRST driver
span of a fresh run reaches the stamp BEFORE any record exists.

Before the run-14 fix the stamp called ``stamp_tick`` unconditionally, which
raised ``FileNotFoundError`` ("no run record for …") inside the journal; the
broad guard swallowed it but logged a FULL-TRACEBACK warning that read as a
crash — the signature that pushed the demo's fresh S2 off block-drive onto
direct CLI (drive.py's docstring records the incident). The fix:

* fresh run, first span (no record, not yet submitted) → a CALM skip (nothing
  in-flight to watch; submit-s2 lays down the initial deadline), no record
  fabricated, no exception;
* record present → the stamp lands as before;
* record ABSENT on a run that HAS been submitted (sidecar carries ``job_ids``)
  → a genuinely-deleted record on an in-flight run, kept LOUD (a real error,
  never masked as a fresh first span).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hpc_agent._kernel.lifecycle.drive import _stamp_driver_tick
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import run_sidecar_path

_RUN_ID = "causal_tune_tree-24b434d2"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _write_sidecar(experiment_dir: Path, run_id: str, *, job_ids: list[str]) -> None:
    path = run_sidecar_path(experiment_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "job_ids": job_ids}), encoding="utf-8")


def _record(experiment_dir: Path, run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-16T00:00:00+00:00",
        experiment_dir=str(experiment_dir),
        status="in_flight",
    )


def test_fresh_first_span_skips_calmly_without_crashing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A fresh run's first span (sidecar resolved, no journal record) drives
    without the FileNotFoundError crash-shape — a calm INFO skip, no traceback,
    and no fabricated record."""
    # S1's resolve leg: a sidecar with NO landed job, and NO journal record.
    _write_sidecar(tmp_path, _RUN_ID, job_ids=[])
    assert load_run(tmp_path, _RUN_ID) is None

    with caplog.at_level(logging.INFO, logger="hpc_agent._kernel.lifecycle.drive"):
        _stamp_driver_tick(tmp_path, _RUN_ID)  # must not raise

    # No stub record fabricated (never invent run state, §5 / the task guard).
    assert load_run(tmp_path, _RUN_ID) is None
    # A calm disclosure, NOT a crash-shaped exc_info traceback.
    records = [r for r in caplog.records if r.name == "hpc_agent._kernel.lifecycle.drive"]
    assert records, "the skip must be disclosed"
    assert all(r.levelno <= logging.INFO for r in records), "fresh-run skip must not be a WARNING"
    assert all(r.exc_info is None for r in records), "fresh-run skip must not log a traceback"


def test_stamp_lands_once_the_record_is_minted(tmp_path: Path) -> None:
    """Once submit-s2 mints the journal record, the very next driver span stamps
    the watchdog deadline (coverage begins at the mint, not before)."""
    upsert_run(tmp_path, _record(tmp_path, _RUN_ID))
    _stamp_driver_tick(tmp_path, _RUN_ID)

    rec = load_run(tmp_path, _RUN_ID)
    assert rec is not None
    assert rec.next_tick_due, "the stamp must land once a record exists"
    assert rec.last_tick_at
    assert rec.next_tick_due > rec.last_tick_at


def test_deleted_record_on_a_submitted_run_stays_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing journal record on a run that HAS been submitted (sidecar carries
    job_ids) is a real error — surfaced as a WARNING, never masked as a fresh
    first span. Still guarded: the tick does not crash."""
    _write_sidecar(tmp_path, _RUN_ID, job_ids=["100"])  # submitted
    assert load_run(tmp_path, _RUN_ID) is None  # but the record is gone

    with caplog.at_level(logging.INFO, logger="hpc_agent._kernel.lifecycle.drive"):
        _stamp_driver_tick(tmp_path, _RUN_ID)  # must not raise

    records = [r for r in caplog.records if r.name == "hpc_agent._kernel.lifecycle.drive"]
    assert records, "a deleted record on a live run must be surfaced"
    assert any(r.levelno >= logging.WARNING for r in records), (
        "a genuinely-missing record on a submitted run must stay LOUD"
    )
