"""Tests for ``parent_records`` — the DAG kernel's lineage accessor.

Same record shape as ``prior_records`` but resolved from an explicit
``parents`` declaration instead of a campaign walk: ordered as declared,
duplicates collapse (parents are a set), and a missing parent fails loud
— the caller named that exact dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent.execution.mapreduce.reduce.history import parent_records
from hpc_agent.state.runs import write_run_sidecar


def _seed_parent(experiment_dir: Path, *, run_id: str, qoi: float) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-06-10T00:00:00Z",
        executor="python3 src/test.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="",
        wave_map={"0": [0]},
        trial_tokens=[run_id],
    )
    task_dir = experiment_dir / "results" / run_id / "task_0"
    task_dir.mkdir(parents=True)
    (task_dir / "metrics.json").write_text(json.dumps({"qoi": qoi}))


def test_records_in_declared_order_with_lineage(tmp_path: Path) -> None:
    _seed_parent(tmp_path, run_id="calib", qoi=1.0)
    _seed_parent(tmp_path, run_id="mesh", qoi=2.0)

    records = parent_records(tmp_path, ["mesh", "calib"])

    assert [r["run_id"] for r in records] == ["mesh", "calib"]
    for r in records:
        assert r["complete"] is True
        assert len(r["result_dirs"]) == 1
        # Path equality, not string suffix — result_dirs are OS-native
        # strings (backslashes on Windows).
        assert Path(r["result_dirs"][0]) == tmp_path / "results" / r["run_id"] / "task_0"
        assert isinstance(r["metrics"], dict)
        assert r["trial_tokens"] == [r["run_id"]]


def test_parent_without_results_is_incomplete(tmp_path: Path) -> None:
    write_run_sidecar(
        tmp_path,
        run_id="pending",
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-06-10T00:00:00Z",
        executor="python3 src/test.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="",
        wave_map={"0": [0]},
    )
    (record,) = parent_records(tmp_path, ["pending"])
    assert record["complete"] is False
    assert record["result_dirs"] == []


def test_duplicates_collapse(tmp_path: Path) -> None:
    _seed_parent(tmp_path, run_id="calib", qoi=1.0)
    records = parent_records(tmp_path, ["calib", "calib"])
    assert [r["run_id"] for r in records] == ["calib"]


def test_missing_parent_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parent_records(tmp_path, ["ghost"])
