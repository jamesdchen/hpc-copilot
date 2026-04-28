"""Stderr/exit-code contract tests for ``hpc_mapreduce.map.dispatch``.

Pins the behaviours that ``/status`` and other observers rely on.  Each test
drives the dispatcher as a subprocess (matching cluster execution) and asserts
on ``returncode`` + stderr substrings only — never on stdout formatting.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

DISPATCH_MODULE = "hpc_mapreduce.map.dispatch"


def _run(
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    full_env = {"PATH": "/usr/bin:/bin:/usr/local/bin", **env}
    return subprocess.run(
        [sys.executable, "-m", DISPATCH_MODULE],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _stub_manifest(tmp_path: Path, schema_version: int, *, with_cmd_sha: bool) -> Path:
    """Write a manifest with one trivial task and return its path."""
    result_dir = tmp_path / "out"
    entry: dict = {
        "cmd": f"{sys.executable} -c 'pass'",
        "result_dir": str(result_dir),
        "params": {"x": "1"},
    }
    if with_cmd_sha:
        # Arbitrary 16-hex — dispatcher must not validate it.
        entry["cmd_sha"] = "deadbeefcafef00d"
    manifest = {
        "schema_version": schema_version,
        "tasks": {"0": entry},
    }
    path = tmp_path / "_hpc_dispatch.json"
    path.write_text(json.dumps(manifest))
    return path


class TestMissingTaskId:
    def test_missing_task_id_env_var(self, tmp_path: Path) -> None:
        manifest_path = _stub_manifest(tmp_path, schema_version=2, with_cmd_sha=True)
        proc = _run(
            cwd=tmp_path,
            env={"HPC_MANIFEST": str(manifest_path)},
        )
        assert proc.returncode != 0
        assert "TASK_ID" in proc.stderr


class TestMissingManifest:
    def test_missing_manifest_file(self, tmp_path: Path) -> None:
        proc = _run(
            cwd=tmp_path,
            env={
                "TASK_ID": "0",
                "HPC_MANIFEST": str(tmp_path / "nope.json"),
            },
        )
        assert proc.returncode != 0
        assert "manifest not found" in proc.stderr


class TestWrongSchemaVersion:
    def test_schema_99_rejected(self, tmp_path: Path) -> None:
        manifest_path = _stub_manifest(tmp_path, schema_version=99, with_cmd_sha=False)
        proc = _run(
            cwd=tmp_path,
            env={"TASK_ID": "0", "HPC_MANIFEST": str(manifest_path)},
        )
        assert proc.returncode != 0
        # The error must reference the schema so users know what to fix.
        assert "schema" in proc.stderr.lower()


class TestSchemaV1BackCompat:
    def test_v1_manifest_without_cmd_sha_runs(self, tmp_path: Path) -> None:
        manifest_path = _stub_manifest(tmp_path, schema_version=1, with_cmd_sha=False)
        proc = _run(
            cwd=tmp_path,
            env={"TASK_ID": "0", "HPC_MANIFEST": str(manifest_path)},
        )
        assert proc.returncode == 0, f"v1 manifest must still dispatch: stderr={proc.stderr!r}"


class TestSchemaV2:
    def test_v2_manifest_with_cmd_sha_runs(self, tmp_path: Path) -> None:
        manifest_path = _stub_manifest(tmp_path, schema_version=2, with_cmd_sha=True)
        proc = _run(
            cwd=tmp_path,
            env={"TASK_ID": "0", "HPC_MANIFEST": str(manifest_path)},
        )
        assert proc.returncode == 0, f"v2 manifest must dispatch cleanly: stderr={proc.stderr!r}"
