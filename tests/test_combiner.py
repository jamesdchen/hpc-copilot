"""Tests for the on-cluster combiner script (hpc_mapreduce/map/combiner.py)."""

from __future__ import annotations

import json
import math
import random

from hpc_mapreduce.job.grid import run_id
from hpc_mapreduce.map.combiner import _neumaier_sum, _run_id, _weighted_mean, main


class TestRunIdMatchesGrid:
    """Verify the combiner's _run_id matches the canonical run_id from grid."""

    def test_simple_params(self):
        params = {"model": "ridge", "horizon": "1"}
        assert _run_id(params) == run_id(params)

    def test_different_keys(self):
        params = {"executor": "xgb", "horizon": "25"}
        assert _run_id(params) == run_id(params)

    def test_special_chars(self):
        params = {"path": "/some/path", "name": "foo bar"}
        assert _run_id(params) == run_id(params)


class TestWeightedMeanSingleEntry:
    def test_single_entry_returned_as_is(self):
        entries = [{"mse": 0.5, "n_samples": 100}]
        result = _weighted_mean(entries, [])
        assert abs(result["mse"] - 0.5) < 1e-9
        assert result["n_samples"] == 100


class TestWeightedMeanEqualWeights:
    def test_equal_weights_simple_average(self):
        entries = [
            {"mse": 0.10, "n_samples": 100},
            {"mse": 0.20, "n_samples": 100},
        ]
        result = _weighted_mean(entries, [])
        assert abs(result["mse"] - 0.15) < 1e-9
        assert result["n_samples"] == 200


class TestWeightedMeanUnequalWeights:
    def test_unequal_weights(self):
        entries = [
            {"mse": 0.10, "n_samples": 100},
            {"mse": 0.30, "n_samples": 300},
        ]
        result = _weighted_mean(entries, [])
        # (0.10*100 + 0.30*300) / 400 = 100/400 = 0.25
        assert abs(result["mse"] - 0.25) < 1e-9
        assert result["n_samples"] == 400


class TestNeumaierSum:
    """The compensated sum is what makes cluster-side reductions reliable."""

    def test_benign_input_matches_plain_sum(self):
        values = [0.1, 0.2, 0.3, 0.4]
        assert abs(_neumaier_sum(values) - sum(values)) < 1e-12

    def test_pathological_cancellation_matches_fsum(self):
        # Classic Kahan regression: huge term, tiny term, huge negative term,
        # repeated many times.  Plain sum drifts; Neumaier tracks fsum.
        seq = [1.0, 1e100, 1.0, -1e100] * 1000
        got = _neumaier_sum(seq)
        want = math.fsum(seq)  # 2000.0 exactly
        assert abs(got - want) < 1e-6
        assert want == 2000.0
        # And demonstrate that plain sum really would have been wrong --
        # guard against someone re-introducing plain sum later.
        assert sum(seq) != want

    def test_empty_sequence_is_zero(self):
        assert _neumaier_sum([]) == 0.0


class TestWeightedMeanOrderInvariant:
    """Running the same entries in a different order must produce the
    same aggregate -- this is the reliability guarantee users care about
    when tasks complete in nondeterministic order on the cluster."""

    def test_order_invariant_on_large_input(self):
        # 500 tasks with varying metric magnitudes and n_samples counts.
        rng = random.Random(42)
        entries_forward = [
            {"mse": rng.uniform(1e-8, 1e4), "n_samples": rng.randint(1, 10_000)}
            for _ in range(500)
        ]
        entries_shuffled = list(entries_forward)
        rng.shuffle(entries_shuffled)

        a = _weighted_mean(entries_forward, [])
        b = _weighted_mean(entries_shuffled, [])

        assert a["n_samples"] == b["n_samples"]
        assert abs(a["mse"] - b["mse"]) < 1e-12

    def test_order_invariant_on_wide_dynamic_range(self):
        # Deliberately extreme magnitudes so plain sum would diverge.
        entries = [
            {"v": 1e12, "n_samples": 1},
            {"v": 1.0, "n_samples": 1},
            {"v": -1e12, "n_samples": 1},
            {"v": 1.0, "n_samples": 1},
        ]
        forward = _weighted_mean(entries, [])
        reverse = _weighted_mean(list(reversed(entries)), [])
        assert abs(forward["v"] - reverse["v"]) < 1e-12


class TestMainEndToEnd:
    def test_main_produces_wave_file(self, tmp_path, monkeypatch):
        # Create two task result dirs with metrics
        r0 = tmp_path / "results" / "task_0"
        r1 = tmp_path / "results" / "task_1"
        r0.mkdir(parents=True)
        r1.mkdir(parents=True)
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.10, "n_samples": 100}))
        (r1 / "metrics.json").write_text(json.dumps({"mse": 0.20, "n_samples": 100}))

        # Build manifest with wave_map
        manifest = {
            "tasks": {
                "0": {
                    "params": {"model": "ridge", "horizon": "1"},
                    "result_dir": str(r0),
                },
                "1": {
                    "params": {"model": "ridge", "horizon": "1"},
                    "result_dir": str(r1),
                },
            },
            "wave_map": {
                "0": ["0", "1"],
            },
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_MANIFEST", str(manifest_path))
        monkeypatch.chdir(tmp_path)

        main()

        out_path = tmp_path / "_combiner" / "wave_0.json"
        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert data["wave"] == 0
        assert data["task_ids"] == ["0", "1"]
        assert len(data["grid_points"]) == 1
        assert data["errors"] == []

        # Check aggregated metrics (equal weights → simple average)
        gp = list(data["grid_points"].values())[0]
        assert abs(gp["mse"] - 0.15) < 1e-9
        assert gp["n_samples"] == 200


class TestMainMissingMetrics:
    def test_missing_metrics_records_error(self, tmp_path, monkeypatch):
        # Only create one task's result dir with metrics
        r0 = tmp_path / "results" / "task_0"
        r0.mkdir(parents=True)
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.10, "n_samples": 50}))

        # Task 1 has no metrics file
        r1 = tmp_path / "results" / "task_1"
        r1.mkdir(parents=True)

        manifest = {
            "tasks": {
                "0": {
                    "params": {"model": "ridge"},
                    "result_dir": str(r0),
                },
                "1": {
                    "params": {"model": "ridge"},
                    "result_dir": str(r1),
                },
            },
            "wave_map": {
                "0": ["0", "1"],
            },
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_MANIFEST", str(manifest_path))
        monkeypatch.chdir(tmp_path)

        # Should not crash
        main()

        data = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        assert len(data["errors"]) == 1
        assert "metrics.json not found" in data["errors"][0]


class TestMainParallelReads:
    """Parallel metrics reads must produce semantically identical output to serial."""

    @staticmethod
    def _build_manifest(tmp_path, n_tasks: int, malformed_tid: str | None = None):
        """Build a manifest with n_tasks across a few grid points.

        If malformed_tid is set, that task's metrics.json is written as
        invalid JSON so the combiner logs an error but doesn't crash.
        """
        tasks: dict = {}
        task_ids: list = []
        # Three grid points cycled through so the weighted mean has
        # something non-trivial to aggregate.
        grid_params = [
            {"model": "ridge", "horizon": "1"},
            {"model": "xgb", "horizon": "1"},
            {"model": "ridge", "horizon": "25"},
        ]
        for i in range(n_tasks):
            tid = str(i)
            rdir = tmp_path / "results" / f"task_{i}"
            rdir.mkdir(parents=True)
            if tid == malformed_tid:
                (rdir / "metrics.json").write_text("{not valid json")
            else:
                # Varying metric values so averaging isn't degenerate.
                (rdir / "metrics.json").write_text(
                    json.dumps({"mse": 0.10 + 0.01 * i, "n_samples": 100 + i})
                )
            tasks[tid] = {
                "params": grid_params[i % len(grid_params)],
                "result_dir": str(rdir),
            }
            task_ids.append(tid)

        manifest = {"tasks": tasks, "wave_map": {"0": task_ids}}
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        return manifest_path

    def _run(self, tmp_path, monkeypatch, manifest_path, max_workers):
        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_MANIFEST", str(manifest_path))
        monkeypatch.chdir(tmp_path)
        main(max_workers=max_workers)
        return json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())

    def test_parallel_matches_serial(self, tmp_path, monkeypatch):
        # Run serial first in its own tmp subdir, then parallel in another.
        serial_dir = tmp_path / "serial"
        parallel_dir = tmp_path / "parallel"
        serial_dir.mkdir()
        parallel_dir.mkdir()

        s_manifest = self._build_manifest(serial_dir, n_tasks=20)
        p_manifest = self._build_manifest(parallel_dir, n_tasks=20)

        serial = self._run(serial_dir, monkeypatch, s_manifest, max_workers=1)
        parallel = self._run(parallel_dir, monkeypatch, p_manifest, max_workers=8)

        # Same grid-point keys
        assert set(serial["grid_points"].keys()) == set(parallel["grid_points"].keys())

        # Same weighted-mean values per grid point
        for key in serial["grid_points"]:
            s_gp = serial["grid_points"][key]
            p_gp = parallel["grid_points"][key]
            assert set(s_gp.keys()) == set(p_gp.keys())
            for metric in s_gp:
                assert abs(s_gp[metric] - p_gp[metric]) < 1e-9, f"mismatch on {key}/{metric}"

        # Same error count (both zero here)
        assert len(serial["errors"]) == len(parallel["errors"]) == 0

    def test_parallel_with_malformed_metrics(self, tmp_path, monkeypatch):
        manifest_path = self._build_manifest(tmp_path, n_tasks=20, malformed_tid="7")
        data = self._run(tmp_path, monkeypatch, manifest_path, max_workers=8)

        # Exactly one error, attributed to task 7, and the run didn't crash.
        assert len(data["errors"]) == 1
        assert "task 7:" in data["errors"][0]
        assert "failed to read metrics.json" in data["errors"][0]

        # The other 19 tasks still aggregated into their grid points.
        total_n = sum(gp["n_samples"] for gp in data["grid_points"].values())
        # n_samples = sum of (100+i) for i in 0..19 except i=7 -> sum(100..119) - 107
        expected = sum(100 + i for i in range(20)) - 107
        assert total_n == expected


class TestMainMultipleGridPoints:
    def test_separate_grid_point_entries(self, tmp_path, monkeypatch):
        r0 = tmp_path / "results" / "ridge"
        r1 = tmp_path / "results" / "xgb"
        r0.mkdir(parents=True)
        r1.mkdir(parents=True)
        (r0 / "metrics.json").write_text(json.dumps({"mse": 0.10, "n_samples": 50}))
        (r1 / "metrics.json").write_text(json.dumps({"mse": 0.30, "n_samples": 50}))

        manifest = {
            "tasks": {
                "0": {
                    "params": {"model": "ridge"},
                    "result_dir": str(r0),
                },
                "1": {
                    "params": {"model": "xgb"},
                    "result_dir": str(r1),
                },
            },
            "wave_map": {
                "0": ["0", "1"],
            },
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        monkeypatch.setenv("HPC_WAVE", "0")
        monkeypatch.setenv("HPC_MANIFEST", str(manifest_path))
        monkeypatch.chdir(tmp_path)

        main()

        data = json.loads((tmp_path / "_combiner" / "wave_0.json").read_text())
        assert len(data["grid_points"]) == 2
        assert data["errors"] == []
