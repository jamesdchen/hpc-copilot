"""Failure-mode coverage for the on-cluster combiner (``hpc_agent/models/mapreduce/combiner.py``).

The combiner is exercised as a fresh subprocess (matching cluster execution).
The new model uses ``--run-id`` + a sidecar at ``.hpc/runs/<id>.json`` plus
the user's ``.hpc/tasks.py`` for per-task kwargs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import hpc_agent


def _materialize_cluster_layout(
    tmp_path: Path,
    *,
    kwargs_per_task: list[dict],
    result_dirs: list[Path],
    run_id: str = "test_run",
) -> Path:
    """Set up tmp_path/.hpc/ as if rsync + deploy_runtime had run.

    Returns the path to the combiner script the test should invoke.
    """
    from tests.conftest import make_sidecar_json, write_hpc_tasks  # noqa: PLC0415

    hpc = tmp_path / ".hpc"
    write_hpc_tasks(hpc, kwargs_per_task)
    make_sidecar_json(
        tmp_path,
        run_id=run_id,
        result_dir_template=str(tmp_path / "task_{task_id}"),
        task_count=len(kwargs_per_task),
        wave_map={"0": list(range(len(kwargs_per_task)))},
    )

    # Place the combiner script as the cluster does.
    combiner_dst = hpc / "_hpc_combiner.py"
    pkg_combiner = Path(hpc_agent.__file__).parent / "models" / "mapreduce" / "combiner.py"
    shutil.copyfile(pkg_combiner, combiner_dst)
    return combiner_dst


def _run_combiner(
    *, combiner_path: Path, cwd: Path, env: dict[str, str], extra_args: list[str] = ()
) -> subprocess.CompletedProcess[str]:
    full_env = {"PATH": "/usr/bin:/bin:/usr/local/bin", **env}
    return subprocess.run(
        [sys.executable, str(combiner_path), *extra_args],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestMissingEnvVars:
    def test_missing_hpc_wave_exits_1(self, tmp_path: Path) -> None:
        combiner = _materialize_cluster_layout(tmp_path, kwargs_per_task=[], result_dirs=[])
        proc = _run_combiner(
            combiner_path=combiner,
            cwd=tmp_path,
            env={"HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode == 1
        assert "HPC_WAVE" in proc.stderr or "--wave" in proc.stderr


class TestMissingSidecar:
    def test_missing_sidecar_exits_1(self, tmp_path: Path) -> None:
        combiner = _materialize_cluster_layout(
            tmp_path, kwargs_per_task=[{}], result_dirs=[tmp_path / "task_0"]
        )
        # Remove the sidecar to force the failure.
        (tmp_path / ".hpc" / "runs" / "test_run.json").unlink()
        proc = _run_combiner(
            combiner_path=combiner,
            cwd=tmp_path,
            env={"HPC_WAVE": "0", "HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode == 1
        assert "sidecar not found" in proc.stderr


class TestMissingWaveInMap:
    def test_wave_not_in_wave_map_lists_available(self, tmp_path: Path) -> None:
        (tmp_path / "task_0").mkdir()
        combiner = _materialize_cluster_layout(
            tmp_path,
            kwargs_per_task=[{"model": "ridge"}],
            result_dirs=[tmp_path / "task_0"],
        )
        proc = _run_combiner(
            combiner_path=combiner,
            cwd=tmp_path,
            env={"HPC_WAVE": "99", "HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode == 1
        assert "99" in proc.stderr
        assert "0" in proc.stderr


class TestMalformedMetricsOneTask:
    def test_one_malformed_metrics_file_does_not_abort(self, tmp_path: Path) -> None:
        r0 = tmp_path / "task_0"
        r1 = tmp_path / "task_1"
        r0.mkdir()
        r1.mkdir()
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.1, "n_samples": 10}))
        (r1 / "metrics.json").write_text("{not valid json")

        combiner = _materialize_cluster_layout(
            tmp_path,
            kwargs_per_task=[{"model": "a"}, {"model": "b"}],
            result_dirs=[r0, r1],
        )
        proc = _run_combiner(
            combiner_path=combiner,
            cwd=tmp_path,
            env={"HPC_WAVE": "0", "HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode == 0, (
            f"combiner must tolerate one malformed file: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

        out = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        assert any("task 1" in e for e in out["errors"]), out["errors"]
        # Distinct kwargs ⇒ distinct grid_keys.
        assert len(out["grid_points"]) == 1
        gp = next(iter(out["grid_points"].values()))
        assert gp["n_samples"] == 10


class TestMissingMetricsOneTask:
    def test_missing_metrics_file_is_per_task_error(self, tmp_path: Path) -> None:
        r0 = tmp_path / "task_0"
        r1 = tmp_path / "task_1"
        r0.mkdir()
        r1.mkdir()
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.2, "n_samples": 20}))

        combiner = _materialize_cluster_layout(
            tmp_path,
            kwargs_per_task=[{"model": "a"}, {"model": "b"}],
            result_dirs=[r0, r1],
        )
        proc = _run_combiner(
            combiner_path=combiner,
            cwd=tmp_path,
            env={"HPC_WAVE": "0", "HPC_RUN_ID": "test_run"},
        )
        assert proc.returncode == 0, (
            f"combiner must tolerate a missing metrics.json: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

        out = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        assert any("task 1" in e and "metrics.json not found" in e for e in out["errors"]), out[
            "errors"
        ]
        assert len(out["grid_points"]) == 1
        gp = next(iter(out["grid_points"].values()))
        assert gp["n_samples"] == 20


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
