"""End-to-end pipeline: build manifest -> dispatch -> check_results_from_manifest.

No scheduler, no network — every task is executed locally by subprocess-invoking
``python -m hpc_mapreduce.map.dispatch`` with ``TASK_ID`` / ``HPC_MANIFEST`` env
vars.  This exercises the full primitive chain in a single pytest run and also
pins the v2 manifest contract (``schema_version == 2`` with a 16-char
``cmd_sha`` on every task).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hpc_mapreduce.job.grid import build_task_manifest
from hpc_mapreduce.reduce.status import check_results_from_manifest

# Tiny stub script: takes arbitrary CLI args, writes a results.csv into the
# env-provided RESULT_DIR (which dispatch sets to the per-task _wip_ dir).
STUB_SCRIPT = """\
import os
import sys

out_dir = os.environ["RESULT_DIR"]
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, "results.csv"), "w") as f:
    f.write("col_a,col_b\\n")
    f.write(",".join(sys.argv[1:]) + "\\n")
"""

HEX16_RE = re.compile(r"^[0-9a-f]{16}$")


def _write_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "stub.py"
    stub.write_text(STUB_SCRIPT)
    return stub


def _run_dispatch(
    tmp_path: Path, manifest_path: Path, task_id: int
) -> subprocess.CompletedProcess[str]:
    """Invoke dispatch as a fresh subprocess (matches cluster execution)."""
    return subprocess.run(
        [sys.executable, "-m", "hpc_mapreduce.map.dispatch"],
        cwd=str(tmp_path),
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "TASK_ID": str(task_id),
            "HPC_MANIFEST": str(manifest_path),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.fixture
def pipeline(tmp_path: Path) -> dict:
    """Build a 4-task manifest, dispatch every task, return key artefacts."""
    stub = _write_stub(tmp_path)
    run_cmd = f"{sys.executable} {stub}"
    result_dir_template = str(tmp_path / "results" / "{run_id}")

    manifest = build_task_manifest(
        run_cmd=run_cmd,
        grid={"alpha": ["0.1", "1.0"], "model": ["ridge", "lasso"]},
        result_dir_template=result_dir_template,
    )

    manifest_path = tmp_path / "_hpc_dispatch.json"
    manifest_path.write_text(json.dumps(manifest))

    # The scheduler templates convert 1-based SGE_TASK_ID / SLURM_ARRAY_TASK_ID
    # to 0-based TASK_ID before invoking dispatch, so the ``TASK_ID`` env var
    # the dispatcher sees always matches the 0-based manifest key directly.
    n_tasks = manifest["total_tasks"]
    for tid in range(n_tasks):
        proc = _run_dispatch(tmp_path, manifest_path, tid)
        assert proc.returncode == 0, (
            f"dispatch failed for task {tid}: stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

    return {
        "tmp_path": tmp_path,
        "manifest_path": manifest_path,
        "manifest": manifest,
    }


class TestPipelineAllComplete:
    def test_every_task_reports_complete(self, pipeline: dict) -> None:
        manifest = pipeline["manifest"]
        results = check_results_from_manifest(manifest, file_glob="*.csv")
        # Manifest task IDs are 0-based; check_results_from_manifest returns 1-based.
        expected = set(range(1, manifest["total_tasks"] + 1))
        assert set(results) == expected, (
            f"expected complete tids {sorted(expected)}, got {sorted(results)}"
        )
        for tid, info in results.items():
            assert info["status"] == "complete", f"task {tid}: {info}"


class TestV2ManifestContract:
    def test_schema_version_is_2_and_every_task_has_cmd_sha(self, pipeline: dict) -> None:
        manifest_path: Path = pipeline["manifest_path"]
        on_disk = json.loads(manifest_path.read_text())
        assert on_disk["schema_version"] == 2
        assert len(on_disk["tasks"]) > 0
        for tid, entry in on_disk["tasks"].items():
            sha = entry.get("cmd_sha")
            assert sha is not None, f"task {tid} is missing cmd_sha"
            assert HEX16_RE.match(sha), f"task {tid}: cmd_sha {sha!r} is not 16 lowercase hex chars"


class TestPoisonedTaskDetected:
    def test_deleting_one_result_flips_status(self, pipeline: dict) -> None:
        manifest = pipeline["manifest"]

        # Initial state: everything complete.
        initial = check_results_from_manifest(manifest, file_glob="*.csv")
        assert len(initial) == manifest["total_tasks"]

        # Poison task 0 by removing its result file.
        task0 = manifest["tasks"]["0"]
        victim = Path(task0["result_dir"]) / "results.csv"
        assert victim.exists(), "pre-condition: stub must have produced results.csv"
        victim.unlink()

        after = check_results_from_manifest(manifest, file_glob="*.csv")
        # Task 0 (manifest) == task 1 (1-based) should now be absent.
        assert 1 not in after, f"task 1 should no longer be complete after poisoning, got {after}"
        # The other tasks remain complete.
        assert len(after) == manifest["total_tasks"] - 1
