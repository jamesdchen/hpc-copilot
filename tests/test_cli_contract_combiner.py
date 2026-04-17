"""Contract test for `python _hpc_combiner.py` (standalone combiner CLI).

Runs the combiner as a subprocess against a fixture manifest and
verifies output JSON shape and --force behavior.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def _build_fixture(workdir: Path) -> Path:
    """Copy combiner.py to workdir as `_hpc_combiner.py`; build a minimal manifest.

    Returns the manifest path.
    """
    repo_root = Path(__file__).resolve().parent.parent
    combiner_src = repo_root / "hpc_mapreduce" / "map" / "combiner.py"
    shutil.copy(combiner_src, workdir / "_hpc_combiner.py")

    r0 = workdir / "task_0"
    r1 = workdir / "task_1"
    r0.mkdir()
    r1.mkdir()
    (r0 / "metrics.json").write_text(json.dumps({"mse": 0.10, "n_samples": 100}))
    (r1 / "metrics.json").write_text(json.dumps({"mse": 0.20, "n_samples": 100}))

    manifest = {
        "tasks": {
            "0": {"params": {"model": "ridge"}, "result_dir": str(r0)},
            "1": {"params": {"model": "ridge"}, "result_dir": str(r1)},
        },
        "wave_map": {"0": ["0", "1"]},
    }
    mpath = workdir / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    return mpath


def _run(workdir: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "_hpc_combiner.py", *extra_args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestCombinerCliContract:
    def test_cli_happy_path_emits_expected_keys(self, tmp_path):
        manifest_path = _build_fixture(tmp_path)
        proc = _run(
            tmp_path,
            "--wave",
            "0",
            "--manifest",
            str(manifest_path),
        )
        assert proc.returncode == 0, proc.stderr

        out_path = tmp_path / "_combiner" / "wave_0.json"
        assert out_path.exists()
        data = json.loads(out_path.read_text())

        assert set(data.keys()) == {"wave", "task_ids", "grid_points", "errors"}
        assert data["wave"] == 0
        assert data["task_ids"] == ["0", "1"]
        assert isinstance(data["grid_points"], dict)
        assert isinstance(data["errors"], list)

    def test_rerun_without_force_refuses(self, tmp_path):
        manifest_path = _build_fixture(tmp_path)
        # First run creates wave_0.json.
        first = _run(tmp_path, "--wave", "0", "--manifest", str(manifest_path))
        assert first.returncode == 0

        # Second run without --force must fail non-zero with a clear stderr.
        second = _run(tmp_path, "--wave", "0", "--manifest", str(manifest_path))
        assert second.returncode != 0
        assert "already exists" in second.stderr.lower() or "force" in second.stderr.lower()

    def test_rerun_with_force_overwrites(self, tmp_path):
        manifest_path = _build_fixture(tmp_path)
        # First run.
        first = _run(tmp_path, "--wave", "0", "--manifest", str(manifest_path))
        assert first.returncode == 0

        # Mutate one task's metrics so we can tell the output was re-generated.
        (tmp_path / "task_0" / "metrics.json").write_text(json.dumps({"mse": 0.99, "n_samples": 1}))

        forced = _run(tmp_path, "--wave", "0", "--manifest", str(manifest_path), "--force")
        assert forced.returncode == 0, forced.stderr

        data = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        gp = next(iter(data["grid_points"].values()))
        # New weighted mean should include the mutated mse value.
        # weights: 1 * 0.99 + 100 * 0.20 = 20.99 / 101 ~= 0.2078
        assert abs(gp["mse"] - (0.99 * 1 + 0.20 * 100) / 101) < 1e-9
