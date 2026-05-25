"""Tests for ``hpc_agent.ops.monitor.summary.monitor_summary``.

The primitive renders the canonical user-facing tick summary by reading
the journal record + the most recent line of
``.hpc/runs/<run_id>.monitor.jsonl``. Tests:

  * no journal record → "unknown" lifecycle
  * empty / missing tick log → "no journal entry yet"
  * in-flight summary renders headline + counts + diff + actions
  * terminal lifecycle states report cleanly
  * armed_hint is None at terminal, present otherwise
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.monitor.summary import monitor_summary
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


def _seed(experiment: Path, run_id: str = "r1", **overrides) -> RunRecord:
    base = dict(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="user@h",
        remote_path="/x",
        job_name="p",
        job_ids=["job_42"],
        total_tasks=10,
        submitted_at="2026-01-01T00:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    base.update(overrides)
    record = RunRecord(**base)
    upsert_run(experiment, record)
    return record


def _write_ticks(experiment: Path, run_id: str, *records: dict) -> None:
    path = experiment / ".hpc" / "runs" / f"{run_id}.monitor.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_no_journal_record_signals_journal_missing(tmp_path: Path, journal_home: Path) -> None:
    out = monitor_summary(tmp_path, run_id="missing")
    # lifecycle_state must be one of the canonical observable_with_timeout
    # states (no 'unknown' alias in envelope.json $defs); journal_missing
    # carries the actual signal.
    assert out["lifecycle_state"] == "abandoned"
    assert out["journal_missing"] is True
    assert "missing" in out["headline"]
    assert out["armed_hint"] is None


def test_journal_only_no_ticks_yet(tmp_path: Path, journal_home: Path) -> None:
    _seed(tmp_path)
    out = monitor_summary(tmp_path, run_id="r1")
    assert out["lifecycle_state"] == "in_flight"
    assert "first tick" in out["headline"]
    assert "no journal entry yet" in out["headline"]


def test_in_flight_renders_counts_and_armed_hint(tmp_path: Path, journal_home: Path) -> None:
    _seed(tmp_path)
    _write_ticks(
        tmp_path,
        "r1",
        {
            "summary": {"complete": 3, "running": 2, "pending": 5, "failed": 0},
            "lifecycle_state": "in_flight",
            "diff_from_prev": {
                # ``monitor_flow`` encodes the delta as a length-1 list
                # whose single element is the count (see
                # ops/monitor_flow.py:540 — ``[cur - prv]``). Not a
                # list of task IDs.
                "newly_complete": [3],
                "newly_failed": [],
                "newly_combined_waves": [],
            },
            "actions": [{"kind": "combine_wave", "wave": 0}],
        },
    )
    out = monitor_summary(tmp_path, run_id="r1")
    assert out["lifecycle_state"] == "in_flight"
    assert "complete=3 running=2 pending=5 failed=0 / total=10" in out["body"]
    assert "diff: +3 complete" in out["body"]
    assert "actions: combine_wave" in out["body"]
    assert out["armed_hint"] is not None


def test_terminal_complete_no_armed_hint(tmp_path: Path, journal_home: Path) -> None:
    _seed(tmp_path)
    _write_ticks(
        tmp_path,
        "r1",
        {
            "summary": {"complete": 10, "running": 0, "pending": 0, "failed": 0},
            "lifecycle_state": "complete",
        },
    )
    out = monitor_summary(tmp_path, run_id="r1")
    assert out["lifecycle_state"] == "complete"
    assert "terminal" in out["headline"]
    assert out["armed_hint"] is None


def test_terminal_failed_renders(tmp_path: Path, journal_home: Path) -> None:
    _seed(tmp_path)
    _write_ticks(
        tmp_path,
        "r1",
        {
            "summary": {"complete": 5, "running": 0, "pending": 0, "failed": 5},
            "lifecycle_state": "failed",
        },
    )
    out = monitor_summary(tmp_path, run_id="r1")
    assert out["lifecycle_state"] == "failed"
    assert out["armed_hint"] is None


def test_combined_and_failed_waves_in_body(tmp_path: Path, journal_home: Path) -> None:
    _seed(tmp_path, combined_waves=[0, 1, 2], failed_waves=[3])
    _write_ticks(
        tmp_path,
        "r1",
        {
            "summary": {"complete": 3, "running": 0, "pending": 6, "failed": 1},
            "lifecycle_state": "in_flight",
        },
    )
    out = monitor_summary(tmp_path, run_id="r1")
    assert "combined_waves: [0, 1, 2]" in out["body"]
    assert "failed_waves: [3]" in out["body"]


def test_malformed_jsonl_line_skipped(tmp_path: Path, journal_home: Path) -> None:
    """A bad line in the tick log shouldn't tank the read; we take the most recent valid line."""
    _seed(tmp_path)
    path = tmp_path / ".hpc" / "runs" / "r1.monitor.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    valid_record = {
        "summary": {"complete": 4, "running": 0, "pending": 6, "failed": 0},
        "lifecycle_state": "in_flight",
    }
    path.write_text(
        "not valid json\n" + json.dumps(valid_record) + "\n",
        encoding="utf-8",
    )
    out = monitor_summary(tmp_path, run_id="r1")
    assert "complete=4" in out["body"]


def test_empty_run_id_raises(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="non-empty"):
        monitor_summary(tmp_path, run_id="")
