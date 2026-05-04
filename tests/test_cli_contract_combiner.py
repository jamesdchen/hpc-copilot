"""Contract test for ``python _hpc_combiner.py`` (standalone combiner CLI).

Runs the combiner as a subprocess against a minimal ``.hpc/`` fixture
and verifies the output JSON shape, ``--force`` behavior, and that
``wave_<N>.json`` is the wave-combined success marker.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def _build_fixture(workdir: Path, *, run_id: str = "test_run") -> Path:
    """Materialize workdir/.hpc/{tasks.py, runs/<id>.json, _hpc_combiner.py}.

    Result dirs and metrics.json files are placed under workdir/. Returns
    the path to the combiner script the test should invoke.
    """
    from tests.conftest import make_sidecar_json, write_hpc_tasks  # noqa: PLC0415

    repo_root = Path(__file__).resolve().parent.parent
    combiner_src = repo_root / "hpc_mapreduce" / "map" / "combiner.py"

    hpc = workdir / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    combiner_dst = hpc / "_hpc_combiner.py"
    shutil.copy(combiner_src, combiner_dst)

    r0 = workdir / "task_0"
    r1 = workdir / "task_1"
    r0.mkdir()
    r1.mkdir()
    (r0 / "metrics.json").write_text(json.dumps({"mse": 0.10, "n_samples": 100}))
    (r1 / "metrics.json").write_text(json.dumps({"mse": 0.20, "n_samples": 100}))

    write_hpc_tasks(hpc, [{"model": "ridge"}, {"model": "ridge"}])
    make_sidecar_json(
        workdir,
        run_id=run_id,
        result_dir_template=str(workdir / "task_{task_id}"),
        task_count=2,
        wave_map={"0": [0, 1]},
    )
    return combiner_dst


def _run(workdir: Path, combiner_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(combiner_path), *extra_args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestCombinerCliContract:
    def test_cli_happy_path_emits_expected_keys(self, tmp_path):
        combiner = _build_fixture(tmp_path)
        proc = _run(tmp_path, combiner, "--wave", "0", "--run-id", "test_run")
        assert proc.returncode == 0, proc.stderr

        out_path = tmp_path / "_combiner" / "wave_0.json"
        assert out_path.exists()
        data = json.loads(out_path.read_text())

        assert set(data.keys()) == {"wave", "run_id", "task_ids", "grid_points", "errors"}
        assert data["wave"] == 0
        assert data["task_ids"] == [0, 1]
        assert isinstance(data["grid_points"], dict)
        assert isinstance(data["errors"], list)

    def test_rerun_without_force_refuses(self, tmp_path):
        combiner = _build_fixture(tmp_path)
        first = _run(tmp_path, combiner, "--wave", "0", "--run-id", "test_run")
        assert first.returncode == 0

        second = _run(tmp_path, combiner, "--wave", "0", "--run-id", "test_run")
        assert second.returncode != 0
        assert "already exists" in second.stderr.lower() or "force" in second.stderr.lower()

    def test_rerun_with_force_overwrites(self, tmp_path):
        combiner = _build_fixture(tmp_path)
        first = _run(tmp_path, combiner, "--wave", "0", "--run-id", "test_run")
        assert first.returncode == 0

        # Mutate one task's metrics so we can tell the output was regenerated.
        (tmp_path / "task_0" / "metrics.json").write_text(
            json.dumps({"mse": 0.99, "n_samples": 1})
        )

        forced = _run(tmp_path, combiner, "--wave", "0", "--run-id", "test_run", "--force")
        assert forced.returncode == 0, forced.stderr

        data = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        gp = next(iter(data["grid_points"].values()))
        # New weighted mean: (0.99 * 1 + 0.20 * 100) / 101
        assert abs(gp["mse"] - (0.99 * 1 + 0.20 * 100) / 101) < 1e-9
