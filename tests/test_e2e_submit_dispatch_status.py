"""End-to-end pipeline: tasks.py + sidecar -> dispatch -> check_results_from_manifest.

No scheduler, no network — every task is executed locally by
subprocess-invoking the deployed ``.hpc/_hpc_dispatch.py`` with
``HPC_TASK_ID`` / ``HPC_RUN_ID`` env vars. Exercises the full primitive
chain in a single pytest run.

The reporting side still consumes a manifest-shaped dict;
``_synthetic_manifest`` builds one from the sidecar + tasks.py the same
way ``hpc_mapreduce.reduce.status._build_synthetic_manifest_from_sidecar``
does on the cluster, so the existing ``check_results_from_manifest``
contract is unchanged.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import hpc_mapreduce
from hpc_mapreduce.reduce.status import check_results_from_manifest

STUB_SCRIPT = """\
import os
import sys

out_dir = os.environ["RESULT_DIR"]
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, "results.csv"), "w") as f:
    f.write("col_a,col_b\\n")
    f.write(",".join(sys.argv[1:]) + "\\n")
"""


def _write_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "stub.py"
    stub.write_text(STUB_SCRIPT)
    return stub


def _materialize_run(
    tmp_path: Path, *, run_id: str = "test_run"
) -> tuple[Path, list[dict], str]:
    """Set up tmp_path/.hpc/{tasks.py, runs/<id>.json, _hpc_dispatch.py}.

    Returns (dispatch_script_path, kwargs_per_task, result_dir_template).
    """
    stub = _write_stub(tmp_path)
    kwargs_per_task = [
        {"alpha": alpha, "model": model}
        for alpha, model in itertools.product(["0.1", "1.0"], ["ridge", "lasso"])
    ]
    result_dir_template = str(tmp_path / "results" / "{alpha}_{model}")
    # Stub takes positional args; user kwargs go on the command line.
    executor = (
        f"{sys.executable} {stub} "
        '"$ALPHA" "$MODEL"'
    )

    hpc = tmp_path / ".hpc"
    (hpc / "runs").mkdir(parents=True)
    (hpc / "tasks.py").write_text(
        f"_TASKS = {json.dumps(kwargs_per_task)}\n"
        "def total(): return len(_TASKS)\n"
        "def resolve(i): return _TASKS[i]\n"
    )
    (hpc / "runs" / f"{run_id}.json").write_text(json.dumps({
        "sidecar_schema_version": 1,
        "run_id": run_id,
        "cmd_sha": "deadbeef" * 8,
        "claude_hpc_version": "0.0.0+test",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": executor,
        "result_dir_template": result_dir_template,
        "task_count": len(kwargs_per_task),
        "tasks_py_sha": "abc",
    }))

    dispatch_dst = hpc / "_hpc_dispatch.py"
    pkg_dispatch = Path(hpc_mapreduce.__file__).parent / "map" / "dispatch.py"
    shutil.copyfile(pkg_dispatch, dispatch_dst)
    return dispatch_dst, kwargs_per_task, result_dir_template


def _synthetic_manifest(
    kwargs_per_task: list[dict],
    *,
    result_dir_template: str,
    run_id: str = "test_run",
) -> dict:
    """Mirror reduce.status._build_synthetic_manifest_from_sidecar locally."""
    tasks = {}
    for i, kwargs in enumerate(kwargs_per_task):
        ctx = {"task_id": i, "run_id": run_id, **kwargs}
        tasks[str(i)] = {
            "result_dir": result_dir_template.format(**ctx),
            "params": kwargs,
            "cmd_sha": None,
        }
    return {
        "schema_version": 2,
        "total_tasks": len(kwargs_per_task),
        "tasks": tasks,
    }


def _run_dispatch(
    tmp_path: Path, dispatch_path: Path, task_id: int, *, run_id: str = "test_run"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(dispatch_path)],
        cwd=str(tmp_path),
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HPC_TASK_ID": str(task_id),
            "HPC_RUN_ID": run_id,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.fixture
def pipeline(tmp_path: Path) -> dict:
    """Materialize a 4-task .hpc/, dispatch every task, return artefacts."""
    dispatch, kwargs_per_task, result_dir_template = _materialize_run(tmp_path)

    for tid in range(len(kwargs_per_task)):
        proc = _run_dispatch(tmp_path, dispatch, tid)
        assert proc.returncode == 0, (
            f"dispatch failed for task {tid}: stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

    manifest = _synthetic_manifest(
        kwargs_per_task, result_dir_template=result_dir_template
    )
    return {
        "tmp_path": tmp_path,
        "manifest": manifest,
        "kwargs_per_task": kwargs_per_task,
    }


class TestPipelineAllComplete:
    def test_every_task_reports_complete(self, pipeline: dict) -> None:
        manifest = pipeline["manifest"]
        results = check_results_from_manifest(manifest, file_glob="*.csv")
        # check_results_from_manifest returns 1-based task IDs.
        expected = set(range(1, manifest["total_tasks"] + 1))
        assert set(results) == expected, (
            f"expected complete tids {sorted(expected)}, got {sorted(results)}"
        )
        for tid, info in results.items():
            assert info["status"] == "complete", f"task {tid}: {info}"


class TestSidecarLayout:
    def test_sidecar_carries_required_fields(self, pipeline: dict) -> None:
        sidecar_path = pipeline["tmp_path"] / ".hpc" / "runs" / "test_run.json"
        on_disk = json.loads(sidecar_path.read_text())
        for key in (
            "sidecar_schema_version", "run_id", "cmd_sha", "claude_hpc_version",
            "submitted_at", "executor", "result_dir_template", "task_count",
            "tasks_py_sha",
        ):
            assert key in on_disk, f"missing sidecar field: {key!r}"


class TestPoisonedTaskDetected:
    def test_deleting_one_result_flips_status(self, pipeline: dict) -> None:
        manifest = pipeline["manifest"]

        initial = check_results_from_manifest(manifest, file_glob="*.csv")
        assert len(initial) == manifest["total_tasks"]

        # Poison task 0 by removing its result file.
        task0 = manifest["tasks"]["0"]
        victim = Path(task0["result_dir"]) / "results.csv"
        assert victim.exists(), "pre-condition: stub must have produced results.csv"
        victim.unlink()

        after = check_results_from_manifest(manifest, file_glob="*.csv")
        assert 1 not in after, f"task 1 should no longer be complete after poisoning, got {after}"
        assert len(after) == manifest["total_tasks"] - 1
