"""Failure-mode coverage for the on-cluster combiner (``hpc_mapreduce/map/combiner.py``).

The combiner is exercised as a fresh subprocess (matching cluster execution).
Only the env-var invocation (``HPC_WAVE`` / ``HPC_MANIFEST``) is pinned here —
argparse flags such as ``--wave``/``--manifest``/``--force`` are owned by a
different test file to avoid ownership conflicts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

COMBINER_MODULE = "hpc_mapreduce.map.combiner"


def _run_combiner(
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Invoke the combiner as a subprocess with *env* (PATH injected)."""
    full_env = {"PATH": "/usr/bin:/bin:/usr/local/bin", **env}
    return subprocess.run(
        [sys.executable, "-m", COMBINER_MODULE],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _write_manifest(path: Path, manifest: dict) -> Path:
    path.write_text(json.dumps(manifest))
    return path


def _basic_manifest(result_dirs: dict[str, Path]) -> dict:
    """Build a minimal manifest with a single wave covering every task id."""
    tasks = {
        tid: {
            "params": {"model": "ridge", "horizon": "1"},
            "result_dir": str(rdir),
        }
        for tid, rdir in result_dirs.items()
    }
    return {
        "tasks": tasks,
        "wave_map": {"0": list(result_dirs.keys())},
    }


class TestMissingEnvVars:
    def test_missing_hpc_wave_exits_1(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(tmp_path / "m.json", _basic_manifest({}))

        proc = _run_combiner(
            cwd=tmp_path,
            env={"HPC_MANIFEST": str(manifest_path)},
        )
        assert proc.returncode == 1
        assert "HPC_WAVE" in proc.stderr


class TestMissingManifest:
    def test_missing_manifest_exits_1(self, tmp_path: Path) -> None:
        proc = _run_combiner(
            cwd=tmp_path,
            env={
                "HPC_WAVE": "0",
                "HPC_MANIFEST": str(tmp_path / "does_not_exist.json"),
            },
        )
        assert proc.returncode == 1
        assert "manifest not found" in proc.stderr


class TestMissingWaveInMap:
    def test_wave_not_in_wave_map_lists_available(self, tmp_path: Path) -> None:
        r0 = tmp_path / "task_0"
        r0.mkdir()
        manifest = _basic_manifest({"0": r0})  # wave_map only contains "0"
        manifest_path = _write_manifest(tmp_path / "m.json", manifest)

        proc = _run_combiner(
            cwd=tmp_path,
            env={
                "HPC_WAVE": "99",
                "HPC_MANIFEST": str(manifest_path),
            },
        )
        assert proc.returncode == 1
        # Error must mention the missing wave and enumerate what IS available.
        assert "99" in proc.stderr
        assert "0" in proc.stderr  # the sole available wave


class TestMalformedMetricsOneTask:
    def test_one_malformed_metrics_file_does_not_abort(self, tmp_path: Path) -> None:
        r0 = tmp_path / "task_0"
        r1 = tmp_path / "task_1"
        r0.mkdir()
        r1.mkdir()
        # Task 0 is fine; task 1 has malformed metrics.json.
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.1, "n_samples": 10}))
        (r1 / "metrics.json").write_text("{not valid json")

        manifest = _basic_manifest({"0": r0, "1": r1})
        manifest_path = _write_manifest(tmp_path / "m.json", manifest)

        proc = _run_combiner(
            cwd=tmp_path,
            env={"HPC_WAVE": "0", "HPC_MANIFEST": str(manifest_path)},
        )
        assert proc.returncode == 0, (
            f"combiner must tolerate one malformed file: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

        out = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        # Malformed task is in errors[] ...
        assert any("task 1" in e for e in out["errors"]), out["errors"]
        # ... but the other task still aggregated into a grid point.
        assert len(out["grid_points"]) == 1
        gp = next(iter(out["grid_points"].values()))
        assert gp["n_samples"] == 10


class TestMissingMetricsOneTask:
    def test_missing_metrics_file_is_per_task_error(self, tmp_path: Path) -> None:
        r0 = tmp_path / "task_0"
        r1 = tmp_path / "task_1"
        r0.mkdir()
        r1.mkdir()
        # Only task 0 has metrics.json; task 1's result_dir is empty.
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.2, "n_samples": 20}))

        manifest = _basic_manifest({"0": r0, "1": r1})
        manifest_path = _write_manifest(tmp_path / "m.json", manifest)

        proc = _run_combiner(
            cwd=tmp_path,
            env={"HPC_WAVE": "0", "HPC_MANIFEST": str(manifest_path)},
        )
        assert proc.returncode == 0, (
            f"combiner must tolerate a missing metrics.json: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

        out = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        assert any("task 1" in e and "metrics.json not found" in e for e in out["errors"]), out[
            "errors"
        ]
        # The good task is still aggregated.
        assert len(out["grid_points"]) == 1
        gp = next(iter(out["grid_points"].values()))
        assert gp["n_samples"] == 20


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
