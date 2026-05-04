"""Stderr/exit-code contract tests for ``claude_hpc.mapreduce.dispatch``.

Pins the behaviours that ``/status`` and other observers rely on. Each
test drives the dispatcher as a subprocess (matching cluster execution)
against a minimal ``.hpc/`` layout under tmp_path, and asserts on
``returncode`` + stderr substrings only — never on stdout formatting.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import claude_hpc


def _run(
    *,
    dispatch_path: Path,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    full_env = {"PATH": "/usr/bin:/bin:/usr/local/bin", **env}
    return subprocess.run(
        [sys.executable, str(dispatch_path)],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _stub_layout(
    tmp_path: Path,
    *,
    run_id: str = "test_run",
    schema_version: int = 1,
    executor: str | None = None,
) -> Path:
    """Materialize tmp_path/.hpc/{tasks.py, runs/<id>.json, _hpc_dispatch.py}.

    Returns the dispatch.py copy that callers should invoke.
    """
    from tests.conftest import make_sidecar_json, write_hpc_tasks  # noqa: PLC0415

    hpc = tmp_path / ".hpc"
    write_hpc_tasks(hpc, [{}])
    make_sidecar_json(
        tmp_path,
        run_id=run_id,
        sidecar_schema_version=schema_version,
        executor=executor or f"{sys.executable} -c 'pass'",
        result_dir_template=str(tmp_path / "out"),
    )

    dispatch_dst = hpc / "_hpc_dispatch.py"
    pkg_dispatch = Path(claude_hpc.__file__).parent / "mapreduce" / "dispatch.py"
    shutil.copyfile(pkg_dispatch, dispatch_dst)
    return dispatch_dst


class TestMissingTaskId:
    def test_missing_task_id_env_var(self, tmp_path: Path) -> None:
        dispatch = _stub_layout(tmp_path)
        proc = _run(
            dispatch_path=dispatch,
            cwd=tmp_path,
            env={"HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode != 0
        assert "HPC_TASK_ID" in proc.stderr


class TestMissingRunId:
    def test_missing_run_id_env_var(self, tmp_path: Path) -> None:
        dispatch = _stub_layout(tmp_path)
        proc = _run(
            dispatch_path=dispatch,
            cwd=tmp_path,
            env={"HPC_TASK_ID": "0"},
        )
        assert proc.returncode != 0
        assert "HPC_RUN_ID" in proc.stderr


class TestMissingSidecar:
    def test_missing_sidecar_file(self, tmp_path: Path) -> None:
        dispatch = _stub_layout(tmp_path)
        # Remove the sidecar to simulate the failure case.
        (tmp_path / ".hpc" / "runs" / "test_run.json").unlink()
        proc = _run(
            dispatch_path=dispatch,
            cwd=tmp_path,
            env={"HPC_TASK_ID": "0", "HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode != 0
        assert "sidecar not found" in proc.stderr


class TestWrongSchemaVersion:
    def test_schema_99_rejected(self, tmp_path: Path) -> None:
        dispatch = _stub_layout(tmp_path, schema_version=99)
        proc = _run(
            dispatch_path=dispatch,
            cwd=tmp_path,
            env={"HPC_TASK_ID": "0", "HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode != 0
        assert "schema" in proc.stderr.lower()


class TestPipelineHappyPath:
    def test_v1_layout_dispatches_cleanly(self, tmp_path: Path) -> None:
        dispatch = _stub_layout(tmp_path, schema_version=1)
        proc = _run(
            dispatch_path=dispatch,
            cwd=tmp_path,
            env={"HPC_TASK_ID": "0", "HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode == 0, f"valid layout must dispatch cleanly: stderr={proc.stderr!r}"
