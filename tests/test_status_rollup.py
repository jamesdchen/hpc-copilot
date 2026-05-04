"""Tests for rollup_by_grid_point and check_results_from_tasks."""

from __future__ import annotations

from unittest.mock import patch

from hpc_mapreduce.reduce.status import (
    check_results_from_tasks,
    report_status_from_tasks,
    rollup_by_grid_point,
    rollup_by_wave,
)


def _tasks_data(tmp_path):
    """Two grid points x two chunks = 4 tasks. Each task has its own result_dir."""
    tasks = {}
    idx = 0
    for horizon in ("1", "5"):
        for chunk in ("a", "b"):
            for model in ("ridge",):
                rdir = tmp_path / f"results_{model}_h{horizon}_{chunk}"
                tasks[str(idx)] = {
                    "cmd": f"python src/{model}.py --horizon {horizon}",
                    "result_dir": str(rdir),
                    "params": {"model": model, "horizon": horizon},
                }
                idx += 1
    return {
        "total_tasks": idx,
        "grid_size": 2,
        "grid_keys": ["model", "horizon"],
        "tasks": tasks,
    }


def test_check_results_from_tasks_finds_completed(tmp_path):
    tasks_data = _tasks_data(tmp_path)
    # Complete tasks 0 and 2 by writing result files
    for tid_str in ("0", "2"):
        rdir = tmp_path / tasks_data["tasks"][tid_str]["result_dir"].split("/")[-1]
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "metrics.json").write_text("{}")

    results = check_results_from_tasks(tasks_data, file_glob="*.json")

    # Per-task dict IDs are 0-based, results dict is 1-based
    assert 1 in results
    assert 3 in results
    assert 2 not in results
    assert 4 not in results
    assert len(results) == 2


def test_check_results_ignores_wip(tmp_path):
    tasks_data = _tasks_data(tmp_path)
    rdir = tmp_path / tasks_data["tasks"]["0"]["result_dir"].split("/")[-1]
    rdir.mkdir(parents=True)
    wip = rdir / "_wip_partial.json"
    wip.write_text("{}")

    results = check_results_from_tasks(tasks_data, file_glob="*.json")

    assert 1 not in results


def test_rollup_groups_by_grid_point(tmp_path):
    tasks_data = _tasks_data(tmp_path)
    # 4 tasks total (horizon=1 and horizon=5, each x 2 chunks)
    report = {
        "tasks": {
            "1": {"status": "complete"},  # task 0: horizon=1, chunk a
            "2": {"status": "complete"},  # task 1: horizon=1, chunk b
            "3": {"status": "running"},  # task 2: horizon=5, chunk a
            "4": {"status": "failed"},  # task 3: horizon=5, chunk b
        },
    }

    rollup = rollup_by_grid_point(report, tasks_data)

    # Two grid points: horizon=1 (both complete) and horizon=5 (one running, one failed)
    assert len(rollup) == 2
    h1 = rollup["horizon=1_model=ridge"]
    h5 = rollup["horizon=5_model=ridge"]
    assert h1["complete"] == 2
    assert h1["total"] == 2
    assert h5["running"] == 1
    assert h5["failed"] == 1
    assert h5["total"] == 2


def test_rollup_handles_empty_params():
    tasks_data = {
        "total_tasks": 1,
        "tasks": {"0": {"cmd": "x", "result_dir": "/tmp/x", "params": {}}},
    }
    report = {"tasks": {"1": {"status": "complete"}}}

    rollup = rollup_by_grid_point(report, tasks_data)

    assert "_" in rollup
    assert rollup["_"]["complete"] == 1


def test_report_status_from_tasks_integrates(tmp_path):
    tasks_data = _tasks_data(tmp_path)
    # Mark task 0 complete by writing a result file
    rdir_name = tasks_data["tasks"]["0"]["result_dir"].split("/")[-1]
    rdir = tmp_path / rdir_name
    rdir.mkdir(parents=True)
    (rdir / "done.json").write_text("{}")

    with (
        patch("hpc_mapreduce.reduce.status.detect_scheduler", return_value="slurm"),
        patch("claude_hpc.infra.backends.query.query_sacct", return_value={}),
    ):
        report = report_status_from_tasks(
            tasks_data,
            job_ids=["12345"],
            scheduler="slurm",
            file_glob="*.json",
        )

    assert report["total_tasks"] == 4
    assert report["summary"]["complete"] == 1
    assert report["summary"]["unknown"] == 3
    assert report["tasks"]["1"]["status"] == "complete"


def test_rollup_by_wave_returns_empty_without_wave_map(tmp_path):
    """A per-task dict without a wave_map (un-batched submissions) yields {}."""
    tasks_data = _tasks_data(tmp_path)  # _tasks_data builds no wave_map
    assert "wave_map" not in tasks_data
    report = {"tasks": {"1": {"status": "complete"}}}
    assert rollup_by_wave(report, tasks_data) == {}


def test_rollup_by_wave_groups_tasks_by_wave():
    """Each wave's bucket counts tasks by status; per-task dict 0-based ↔ report 1-based."""
    tasks_data = {
        "total_tasks": 4,
        "tasks": {
            "0": {"params": {}, "cmd": "x", "result_dir": "/x"},
            "1": {"params": {}, "cmd": "x", "result_dir": "/x"},
            "2": {"params": {}, "cmd": "x", "result_dir": "/x"},
            "3": {"params": {}, "cmd": "x", "result_dir": "/x"},
        },
        # Wave 0 = tasks 0,1; wave 1 = tasks 2,3
        "wave_map": {"0": ["0", "1"], "1": ["2", "3"]},
    }
    # Report uses 1-based task ids
    report = {
        "tasks": {
            "1": {"status": "complete"},  # task 0
            "2": {"status": "complete"},  # task 1
            "3": {"status": "running"},   # task 2
            "4": {"status": "failed"},    # task 3
        }
    }

    waves = rollup_by_wave(report, tasks_data)

    assert set(waves.keys()) == {"0", "1"}
    assert waves["0"]["complete"] == 2
    assert waves["0"]["total"] == 2
    assert waves["1"]["running"] == 1
    assert waves["1"]["failed"] == 1
    assert waves["1"]["total"] == 2


def test_rollup_by_wave_marks_missing_tasks_unknown():
    """Tasks listed in wave_map but absent from the report count as unknown."""
    tasks_data = {
        "tasks": {"0": {"params": {}, "cmd": "x", "result_dir": "/x"}},
        "wave_map": {"0": ["0"]},
    }
    report = {"tasks": {}}  # empty -> task 0 not present
    waves = rollup_by_wave(report, tasks_data)
    assert waves["0"]["unknown"] == 1
    assert waves["0"]["total"] == 1
