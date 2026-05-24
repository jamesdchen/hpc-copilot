"""Contract test for `python -m hpc_agent.models.mapreduce.reduce.status`.

Asserts stdout JSON has exactly the pinned 4 top-level keys
(summary, tasks, rollup, errors) with the right types, so the LLM
orchestrator's parser can rely on that shape on every poll.

The CLI reads ``.hpc/runs/<run_id>.json`` for the run sidecar and
``.hpc/tasks.py`` for the per-task kwargs; both must exist relative
to the cwd the subprocess runs in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _build_minimal_run(tmp_path: Path, *, run_id: str = "test_run") -> tuple[Path, Path]:
    """Materialize tmp_path/.hpc/{tasks.py, runs/<id>.json}.

    Three tasks over two grid points (model=ridge for tasks 0–1,
    model=xgb for task 2); only task 0's result_dir has a completed
    artifact, so the reporter should mark task 1 complete (1-based).
    Returns (cwd_for_subprocess, sidecar_path).
    """
    r0 = tmp_path / "task_0"
    r1 = tmp_path / "task_1"
    r2 = tmp_path / "task_2"
    for r in (r0, r1, r2):
        r.mkdir()
    (r0 / "done.json").write_text("{}")  # mark task 0 complete

    from tests.conftest import make_sidecar_json, write_hpc_tasks  # noqa: PLC0415

    hpc = tmp_path / ".hpc"
    write_hpc_tasks(
        hpc,
        [{"model": "ridge"}, {"model": "ridge"}, {"model": "xgb"}],
    )
    # result_dir_template renders to tmp_path/task_0, task_1, task_2 by
    # using {task_id} (consuming the literal index) — matches the dirs
    # we created above.
    sidecar = make_sidecar_json(
        tmp_path,
        run_id=run_id,
        result_dir_template=str(tmp_path / "task_{task_id}"),
        task_count=3,
    )
    return tmp_path, sidecar


def _run_status(cwd: Path, run_id: str = "test_run") -> tuple[int, str, str]:
    """Invoke the status CLI as a subprocess from *cwd*."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hpc_agent.models.mapreduce.reduce.status",
            "--run-id",
            run_id,
            "--scheduler",
            "slurm",
            "--file-glob",
            "*.json",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestStatusCliContract:
    def test_stdout_is_valid_json_with_pinned_top_level_keys(self, tmp_path):
        cwd, _ = _build_minimal_run(tmp_path)
        rc, out, err = _run_status(cwd)
        assert rc == 0, f"stderr={err}"

        doc = json.loads(out)
        assert isinstance(doc, dict)

        # Four pinned keys must all be present.
        for k in ("summary", "tasks", "rollup", "errors"):
            assert k in doc, f"missing pinned key: {k}"

    def test_top_level_types_match_contract(self, tmp_path):
        cwd, _ = _build_minimal_run(tmp_path)
        rc, out, _ = _run_status(cwd)
        assert rc == 0
        doc = json.loads(out)

        # summary: 5 int keys
        summary = doc["summary"]
        assert isinstance(summary, dict)
        for k in ("complete", "running", "pending", "failed", "unknown"):
            assert k in summary, f"summary missing key: {k}"
            assert isinstance(summary[k], int), f"summary[{k}] not int"

        # tasks: dict
        assert isinstance(doc["tasks"], dict)

        # rollup: dict
        assert isinstance(doc["rollup"], dict)

        # errors: list of {code, detail} dicts (possibly empty)
        assert isinstance(doc["errors"], list)
        for e in doc["errors"]:
            assert isinstance(e, dict)
            assert "code" in e and isinstance(e["code"], str)
            assert "detail" in e and isinstance(e["detail"], str)

    def test_per_task_cmd_sha_is_null_in_new_model(self, tmp_path):
        """``cmd_sha`` lives at the run level (sidecar) now — per-task
        entries always serialize ``null``."""
        cwd, _ = _build_minimal_run(tmp_path)
        rc, out, _ = _run_status(cwd)
        assert rc == 0
        doc = json.loads(out)
        for tid, info in doc["tasks"].items():
            assert info.get("cmd_sha") is None, f"task {tid} cmd_sha not null"

    def test_missing_sidecar_still_emits_pinned_shape(self, tmp_path):
        """Even on a sidecar lookup miss, the JSON envelope must keep
        its 4-key shape so the orchestrator can parse it unconditionally."""
        # Materialize .hpc/tasks.py but no sidecar.
        hpc = tmp_path / ".hpc"
        (hpc / "runs").mkdir(parents=True)
        (hpc / "tasks.py").write_text(
            "_TASKS = [{}]\ndef total(): return 1\ndef resolve(i): return _TASKS[0]\n"
        )
        rc, out, _ = _run_status(tmp_path, run_id="does-not-exist")
        assert rc != 0
        doc = json.loads(out)
        for k in ("summary", "tasks", "rollup", "errors"):
            assert k in doc
        assert doc["errors"], "expected at least one error for missing sidecar"
