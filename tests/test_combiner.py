"""Tests for the on-cluster combiner (hpc_mapreduce/map/combiner.py).

The combiner imports the user's ``.hpc/tasks.py`` and reads the per-run
sidecar at ``.hpc/runs/<run_id>.json`` for the wave_map and result_dir
template. Pure-function tests (Neumaier sum, weighted mean, grid-key
derivation) don't need either, and live above.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

from claude_hpc.mapreduce import combiner as combiner_mod
from claude_hpc.mapreduce.combiner import _grid_key, _neumaier_sum, _weighted_mean, main


def _scaffold(
    tmp_path: Path,
    *,
    kwargs_per_task: list[dict],
    result_dir_template: str | None = None,
    run_id: str = "test_run",
    wave_map: dict[str, list[int]] | None = None,
) -> Path:
    """Materialize tmp_path/.hpc/{tasks.py, runs/<run_id>.json}.

    Returns the .hpc dir so callers can monkeypatch combiner.__file__.
    """
    from tests.conftest import make_sidecar_json, write_hpc_tasks  # noqa: PLC0415

    hpc = tmp_path / ".hpc"
    write_hpc_tasks(hpc, kwargs_per_task)
    if wave_map is None:
        wave_map = {"0": list(range(len(kwargs_per_task)))}
    if result_dir_template is None:
        result_dir_template = str(tmp_path / "results" / "task_{task_id}")
    make_sidecar_json(
        tmp_path,
        run_id=run_id,
        result_dir_template=result_dir_template,
        task_count=len(kwargs_per_task),
        wave_map={k: list(v) for k, v in wave_map.items()},
    )
    return hpc


def _patch_sibling_lookup(monkeypatch, hpc: Path) -> None:
    """Make combiner.main treat hpc/ as the dir containing __file__."""
    monkeypatch.setattr(
        combiner_mod, "__file__", str(hpc / "_hpc_combiner.py"), raising=False
    )


# ─── Pure-function tests ────────────────────────────────────────────────────


class TestGridKeyStability:
    def test_simple_params(self):
        assert _grid_key({"model": "ridge", "horizon": "1"}) == "ridge_1"

    def test_special_chars_sanitized(self):
        # "/" and " " aren't in [A-Za-z0-9.-]; both become "_".
        assert _grid_key({"path": "/some/path", "name": "foo bar"}) == "_some_path_foo_bar"

    def test_handles_non_string_values(self):
        # The new combiner casts via str(), so numeric kwargs work.
        assert _grid_key({"seed": 42, "model": "v1"}) == "42_v1"


class TestWeightedMeanSingleEntry:
    def test_single_entry_returned_as_is(self):
        result = _weighted_mean([{"mse": 0.5, "n_samples": 100}])
        assert abs(result["mse"] - 0.5) < 1e-9
        assert result["n_samples"] == 100


class TestWeightedMeanEqualWeights:
    def test_equal_weights_simple_average(self):
        entries = [
            {"mse": 0.10, "n_samples": 100},
            {"mse": 0.20, "n_samples": 100},
        ]
        result = _weighted_mean(entries)
        assert abs(result["mse"] - 0.15) < 1e-9
        assert result["n_samples"] == 200


class TestWeightedMeanUnequalWeights:
    def test_unequal_weights(self):
        entries = [
            {"mse": 0.10, "n_samples": 100},
            {"mse": 0.20, "n_samples": 300},
        ]
        result = _weighted_mean(entries)
        # weighted mean = (0.10*100 + 0.20*300) / 400 = 70/400 = 0.175
        assert abs(result["mse"] - 0.175) < 1e-9
        assert result["n_samples"] == 400


class TestNeumaierSum:
    def test_benign_input_matches_plain_sum(self):
        xs = [0.1, 0.2, 0.3, 0.4]
        assert abs(_neumaier_sum(xs) - sum(xs)) < 1e-12

    def test_pathological_cancellation_matches_fsum(self):
        random.seed(0)
        xs = [random.gauss(0, 1) for _ in range(1000)]
        # Add huge magnitudes that nearly cancel, exposing naive sum's drift.
        xs.extend([1e16, -1e16, 1.0])
        assert abs(_neumaier_sum(xs) - math.fsum(xs)) < 1e-9

    def test_empty_sequence_is_zero(self):
        assert _neumaier_sum([]) == 0.0


# ─── End-to-end via combiner.main() ─────────────────────────────────────────


class TestMainEndToEnd:
    def test_main_produces_wave_file(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        r0 = result_root / "task_0"
        r1 = result_root / "task_1"
        r0.mkdir(parents=True)
        r1.mkdir(parents=True)
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.10, "n_samples": 100}))
        (r1 / "metrics.json").write_text(json.dumps({"mse": 0.20, "n_samples": 100}))

        hpc = _scaffold(
            tmp_path,
            # Two tasks with the same kwargs ⇒ same grid_key ⇒ one grid point.
            kwargs_per_task=[{"model": "ridge", "horizon": "1"},
                             {"model": "ridge", "horizon": "1"}],
        )
        _patch_sibling_lookup(monkeypatch, hpc)
        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)

        main()

        out = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        assert out["wave"] == 0
        assert out["task_ids"] == [0, 1]
        assert len(out["grid_points"]) == 1
        assert out["errors"] == []
        gp = next(iter(out["grid_points"].values()))
        assert abs(gp["mse"] - 0.15) < 1e-9
        assert gp["n_samples"] == 200


class TestMainMissingMetrics:
    def test_missing_metrics_records_error(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        r0 = result_root / "task_0"
        r0.mkdir(parents=True)
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.10, "n_samples": 50}))
        # task 1 dir has no metrics.json
        (result_root / "task_1").mkdir()

        hpc = _scaffold(
            tmp_path,
            kwargs_per_task=[{"model": "ridge"}, {"model": "ridge"}],
        )
        _patch_sibling_lookup(monkeypatch, hpc)
        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)

        main()

        data = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        assert len(data["errors"]) == 1
        assert "metrics.json not found" in data["errors"][0]


class TestMainParallelReads:
    """Parallel metrics reads must produce semantically identical output to serial."""

    def _seed(self, tmp_path: Path, n_tasks: int, malformed_tid: int | None = None) -> Path:
        """Set up tmp_path/.hpc/, result dirs, return .hpc path."""
        result_root = tmp_path / "results"
        kwargs_per_task = []
        grid_choices = [
            {"model": "ridge", "horizon": "1"},
            {"model": "xgb", "horizon": "1"},
            {"model": "ridge", "horizon": "25"},
        ]
        for i in range(n_tasks):
            kwargs_per_task.append(grid_choices[i % len(grid_choices)])
            rdir = result_root / f"task_{i}"
            rdir.mkdir(parents=True)
            if i == malformed_tid:
                (rdir / "metrics.json").write_text("{not valid json")
            else:
                (rdir / "metrics.json").write_text(
                    json.dumps({"mse": 0.10 + 0.01 * i, "n_samples": 100 + i})
                )
        return _scaffold(tmp_path, kwargs_per_task=kwargs_per_task)

    def _run(self, tmp_path, monkeypatch, max_workers):
        hpc = tmp_path / ".hpc"
        _patch_sibling_lookup(monkeypatch, hpc)
        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)
        main(max_workers=max_workers)
        return json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())

    def test_parallel_matches_serial(self, tmp_path, monkeypatch):
        serial_dir = tmp_path / "serial"
        parallel_dir = tmp_path / "parallel"
        serial_dir.mkdir()
        parallel_dir.mkdir()
        self._seed(serial_dir, n_tasks=20)
        self._seed(parallel_dir, n_tasks=20)

        serial = self._run(serial_dir, monkeypatch, max_workers=1)
        parallel = self._run(parallel_dir, monkeypatch, max_workers=8)

        assert set(serial["grid_points"].keys()) == set(parallel["grid_points"].keys())
        for key in serial["grid_points"]:
            s_gp = serial["grid_points"][key]
            p_gp = parallel["grid_points"][key]
            assert set(s_gp.keys()) == set(p_gp.keys())
            for metric in s_gp:
                assert abs(s_gp[metric] - p_gp[metric]) < 1e-9, f"mismatch on {key}/{metric}"
        assert len(serial["errors"]) == len(parallel["errors"]) == 0

    def test_parallel_with_malformed_metrics(self, tmp_path, monkeypatch):
        self._seed(tmp_path, n_tasks=20, malformed_tid=7)
        data = self._run(tmp_path, monkeypatch, max_workers=8)

        assert len(data["errors"]) == 1
        assert "task 7:" in data["errors"][0]
        assert "failed to read metrics.json" in data["errors"][0]

        total_n = sum(gp["n_samples"] for gp in data["grid_points"].values())
        expected = sum(100 + i for i in range(20)) - 107
        assert total_n == expected


class TestMainMultipleGridPoints:
    def test_separate_grid_point_entries(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        r0 = result_root / "task_0"
        r1 = result_root / "task_1"
        r0.mkdir(parents=True)
        r1.mkdir(parents=True)
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.10, "n_samples": 50}))
        (r1 / "metrics.json").write_text(json.dumps({"mse": 0.30, "n_samples": 50}))

        hpc = _scaffold(
            tmp_path,
            kwargs_per_task=[{"model": "ridge"}, {"model": "xgb"}],
        )
        _patch_sibling_lookup(monkeypatch, hpc)
        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)

        main()

        data = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        assert len(data["grid_points"]) == 2
        assert data["errors"] == []


class TestMainWritesOutputAtomically:
    """``_combiner/wave_<N>.json`` existence is the wave-combined success
    marker. A combiner killed mid-write must not leave a half-written
    file masquerading as success.
    """

    def _seed(self, tmp_path: Path) -> Path:
        rdir = tmp_path / "results" / "task_0"
        rdir.mkdir(parents=True)
        (rdir / "metrics.json").write_text(json.dumps({"mse": 0.1, "n_samples": 1}))
        return _scaffold(tmp_path, kwargs_per_task=[{"model": "ridge"}])

    def test_partial_write_failure_leaves_no_wave_file(self, tmp_path, monkeypatch):
        from unittest.mock import patch as _patch

        hpc = self._seed(tmp_path)
        _patch_sibling_lookup(monkeypatch, hpc)
        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("walltime")

        with _patch.object(combiner_mod.json, "dump", side_effect=boom):
            with __import__("pytest").raises(RuntimeError):
                combiner_mod.main()

        out = tmp_path / "_combiner" / "wave_0.json"
        assert not out.exists()
        leftovers = list((tmp_path / "_combiner").glob("wave_*.json.tmp"))
        assert leftovers == []

    def test_successful_write_produces_parseable_json(self, tmp_path, monkeypatch):
        hpc = self._seed(tmp_path)
        _patch_sibling_lookup(monkeypatch, hpc)
        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)

        main()

        out = tmp_path / "_combiner" / "wave_0.json"
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["wave"] == 0
