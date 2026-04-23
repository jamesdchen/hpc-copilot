"""Tests for rollup_by_grid_point and check_results_from_manifest."""

from __future__ import annotations

from unittest.mock import patch

from hpc_mapreduce.reduce.status import (
    check_results_from_manifest,
    report_status_from_manifest,
    rollup_by_grid_point,
)


def _manifest(tmp_path):
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


def test_check_results_from_manifest_finds_completed(tmp_path):
    manifest = _manifest(tmp_path)
    # Complete tasks 0 and 2 by writing result files
    for tid_str in ("0", "2"):
        rdir = tmp_path / manifest["tasks"][tid_str]["result_dir"].split("/")[-1]
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "metrics.json").write_text("{}")

    results = check_results_from_manifest(manifest, file_glob="*.json")

    # Manifest IDs are 0-based, results dict is 1-based
    assert 1 in results
    assert 3 in results
    assert 2 not in results
    assert 4 not in results
    assert len(results) == 2


def test_check_results_ignores_wip(tmp_path):
    manifest = _manifest(tmp_path)
    rdir = tmp_path / manifest["tasks"]["0"]["result_dir"].split("/")[-1]
    rdir.mkdir(parents=True)
    wip = rdir / "_wip_partial.json"
    wip.write_text("{}")

    results = check_results_from_manifest(manifest, file_glob="*.json")

    assert 1 not in results


def test_rollup_groups_by_grid_point(tmp_path):
    manifest = _manifest(tmp_path)
    # 4 tasks total (horizon=1 and horizon=5, each x 2 chunks)
    report = {
        "tasks": {
            "1": {"status": "complete"},  # manifest 0: horizon=1, chunk a
            "2": {"status": "complete"},  # manifest 1: horizon=1, chunk b
            "3": {"status": "running"},  # manifest 2: horizon=5, chunk a
            "4": {"status": "failed"},  # manifest 3: horizon=5, chunk b
        },
    }

    rollup = rollup_by_grid_point(report, manifest)

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
    manifest = {
        "total_tasks": 1,
        "tasks": {"0": {"cmd": "x", "result_dir": "/tmp/x", "params": {}}},
    }
    report = {"tasks": {"1": {"status": "complete"}}}

    rollup = rollup_by_grid_point(report, manifest)

    assert "_" in rollup
    assert rollup["_"]["complete"] == 1


def test_report_status_from_manifest_integrates(tmp_path):
    manifest = _manifest(tmp_path)
    # Mark task 0 complete by writing a result file
    rdir_name = manifest["tasks"]["0"]["result_dir"].split("/")[-1]
    rdir = tmp_path / rdir_name
    rdir.mkdir(parents=True)
    (rdir / "done.json").write_text("{}")

    with (
        patch("hpc_mapreduce.reduce.status.detect_scheduler", return_value="slurm"),
        patch("hpc_mapreduce.infra.backends.query.query_sacct", return_value={}),
    ):
        report = report_status_from_manifest(
            manifest,
            job_ids=["12345"],
            scheduler="slurm",
            file_glob="*.json",
        )

    assert report["total_tasks"] == 4
    assert report["summary"]["complete"] == 1
    assert report["summary"]["unknown"] == 3
    assert report["tasks"]["1"]["status"] == "complete"
