"""Tests for reduce_by_grid_point — group tasks by ``params`` and reduce each group."""

from __future__ import annotations

import json
import math
import random
from typing import TYPE_CHECKING

from hpc_mapreduce.reduce.metrics import (
    _neumaier_sum,
    reduce_by_grid_point,
    reduce_metrics,
    reduce_partials,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_metrics(result_dir: Path, metrics: dict) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "metrics.json").write_text(json.dumps(metrics))


class TestReduceByGridPoint:
    def test_groups_by_grid_point(self, tmp_path):
        """Tasks with same params are grouped; metrics averaged across periods."""
        r1 = tmp_path / "results" / "ridge_1"
        r2 = tmp_path / "results" / "ridge_1b"

        _write_metrics(r1, {"mse": 0.10, "n_samples": 100})
        _write_metrics(r2, {"mse": 0.20, "n_samples": 100})

        tasks_data = {
            "tasks": {
                "0": {
                    "params": {"model": "ridge", "horizon": "1"},
                    "result_dir": str(r1),
                    "period": {"start": "2020-01-01", "end": "2020-06-30"},
                },
                "1": {
                    "params": {"model": "ridge", "horizon": "1"},
                    "result_dir": str(r2),
                    "period": {"start": "2020-07-01", "end": "2020-12-31"},
                },
            }
        }

        result = reduce_by_grid_point(tasks_data)
        assert len(result) == 1
        key = list(result.keys())[0]
        # Weighted average of 0.10 and 0.20 with equal weights
        assert abs(result[key]["mse"] - 0.15) < 1e-9
        assert result[key]["n_samples"] == 200

    def test_multiple_grid_points(self, tmp_path):
        """Different grid points produce separate entries."""
        r1 = tmp_path / "ridge"
        r2 = tmp_path / "xgb"

        _write_metrics(r1, {"mse": 0.10, "n_samples": 50})
        _write_metrics(r2, {"mse": 0.30, "n_samples": 50})

        tasks_data = {
            "tasks": {
                "0": {"params": {"model": "ridge"}, "result_dir": str(r1)},
                "1": {"params": {"model": "xgb"}, "result_dir": str(r2)},
            }
        }

        result = reduce_by_grid_point(tasks_data)
        assert len(result) == 2

    def test_missing_metrics_returns_empty(self, tmp_path):
        """Grid points with no metrics.json get empty dicts."""
        tasks_data = {
            "tasks": {
                "0": {
                    "params": {"model": "ridge"},
                    "result_dir": str(tmp_path / "nonexistent"),
                },
            }
        }
        result = reduce_by_grid_point(tasks_data)
        assert len(result) == 1
        assert result[list(result.keys())[0]] == {}

    def test_unequal_period_weights(self, tmp_path):
        """Periods with different n_samples are weighted correctly."""
        r1 = tmp_path / "a"
        r2 = tmp_path / "b"

        _write_metrics(r1, {"mse": 0.10, "n_samples": 100})
        _write_metrics(r2, {"mse": 0.30, "n_samples": 300})

        tasks_data = {
            "tasks": {
                "0": {
                    "params": {"model": "ridge"},
                    "result_dir": str(r1),
                    "period": {"start": "2020-01-01", "end": "2020-03-31"},
                },
                "1": {
                    "params": {"model": "ridge"},
                    "result_dir": str(r2),
                    "period": {"start": "2020-04-01", "end": "2020-12-31"},
                },
            }
        }

        result = reduce_by_grid_point(tasks_data)
        key = list(result.keys())[0]
        # Weighted: (0.10*100 + 0.30*300) / 400 = 100/400 = 0.25
        assert abs(result[key]["mse"] - 0.25) < 1e-9
        assert result[key]["n_samples"] == 400


def _write_wave(combiner_dir: Path, wave: int, grid_points: dict) -> None:
    combiner_dir.mkdir(parents=True, exist_ok=True)
    (combiner_dir / f"wave_{wave}.json").write_text(
        json.dumps(
            {
                "wave": wave,
                "task_ids": [],
                "grid_points": grid_points,
                "errors": [],
            }
        )
    )


class TestReducePartials:
    def test_single_wave(self, tmp_path):
        combiner_dir = tmp_path / "_combiner"
        _write_wave(combiner_dir, 0, {"ridge_1": {"mse": 0.10, "n_samples": 100}})
        result = reduce_partials(combiner_dir)
        assert len(result) == 1
        assert abs(result["ridge_1"]["mse"] - 0.10) < 1e-9
        assert result["ridge_1"]["n_samples"] == 100

    def test_multi_wave_weighted_merge(self, tmp_path):
        combiner_dir = tmp_path / "_combiner"
        _write_wave(combiner_dir, 0, {"ridge_1": {"mse": 0.10, "n_samples": 100}})
        _write_wave(combiner_dir, 1, {"ridge_1": {"mse": 0.30, "n_samples": 300}})
        result = reduce_partials(combiner_dir)
        assert len(result) == 1
        assert abs(result["ridge_1"]["mse"] - 0.25) < 1e-9
        assert result["ridge_1"]["n_samples"] == 400

    def test_disjoint_grid_points(self, tmp_path):
        combiner_dir = tmp_path / "_combiner"
        _write_wave(combiner_dir, 0, {"ridge_1": {"mse": 0.10, "n_samples": 50}})
        _write_wave(combiner_dir, 1, {"xgb_25": {"mse": 0.30, "n_samples": 50}})
        result = reduce_partials(combiner_dir)
        assert len(result) == 2
        assert "ridge_1" in result
        assert "xgb_25" in result

    def test_missing_wave_file_returns_empty(self, tmp_path):
        combiner_dir = tmp_path / "_combiner"
        combiner_dir.mkdir(parents=True)
        result = reduce_partials(combiner_dir)
        assert result == {}

    def test_matches_reduce_by_grid_point(self, tmp_path):
        """Cross-validation: reduce_partials and reduce_by_grid_point agree."""
        # Set up result dirs with metrics files
        r1 = tmp_path / "results" / "ridge_a"
        r2 = tmp_path / "results" / "ridge_b"
        _write_metrics(r1, {"mse": 0.10, "n_samples": 100})
        _write_metrics(r2, {"mse": 0.30, "n_samples": 300})

        # reduce_by_grid_point path
        tasks_data = {
            "tasks": {
                "0": {
                    "params": {"model": "ridge"},
                    "result_dir": str(r1),
                    "period": {"start": "2020-01-01", "end": "2020-03-31"},
                },
                "1": {
                    "params": {"model": "ridge"},
                    "result_dir": str(r2),
                    "period": {"start": "2020-04-01", "end": "2020-12-31"},
                },
            }
        }
        bt_result = reduce_by_grid_point(tasks_data)

        # reduce_partials path — simulate combiner output for same data
        combiner_dir = tmp_path / "_combiner"
        # Wave 0 has task 0, wave 1 has task 1.
        # Local copy of the combiner's _grid_key semantics, kept inline so
        # the test stays self-contained.
        import re as _re

        def run_id(params):
            raw = "_".join(str(v) for v in params.values())
            return _re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)

        grid_key = run_id({"model": "ridge"})
        _write_wave(combiner_dir, 0, {grid_key: {"mse": 0.10, "n_samples": 100}})
        _write_wave(combiner_dir, 1, {grid_key: {"mse": 0.30, "n_samples": 300}})
        pp_result = reduce_partials(combiner_dir)

        # Both should produce the same aggregated metrics
        assert set(bt_result.keys()) == set(pp_result.keys())
        for key in bt_result:
            for metric in bt_result[key]:
                assert abs(bt_result[key][metric] - pp_result[key][metric]) < 1e-9


class TestNeumaierSumReduce:
    """Numerical stability of the reduce-side weighted mean."""

    def test_matches_fsum_on_cancellation_heavy_input(self):
        seq = [1.0, 1e100, 1.0, -1e100] * 1000
        assert abs(_neumaier_sum(seq) - math.fsum(seq)) < 1e-6
        # math.fsum is exact here (2000.0) so we can assert the expected value.
        assert math.fsum(seq) == 2000.0

    def test_empty_returns_zero(self):
        assert _neumaier_sum([]) == 0.0


class TestReduceMetricsOrderInvariant:
    """reduce_metrics must produce the same aggregate regardless of the
    order the task result_dirs are scanned in."""

    def test_reduce_metrics_order_invariant(self, tmp_path):
        rng = random.Random(7)
        dirs = []
        for i in range(200):
            d = tmp_path / f"task_{i}"
            _write_metrics(
                d,
                {
                    "mse": rng.uniform(1e-6, 1e3),
                    "n_samples": rng.randint(1, 1_000),
                },
            )
            dirs.append(d)

        forward = reduce_metrics(dirs)
        reverse = reduce_metrics(list(reversed(dirs)))
        shuffled = list(dirs)
        rng.shuffle(shuffled)
        random_order = reduce_metrics(shuffled)

        assert forward["n_samples"] == reverse["n_samples"] == random_order["n_samples"]
        assert abs(forward["mse"] - reverse["mse"]) < 1e-12
        assert abs(forward["mse"] - random_order["mse"]) < 1e-12


class TestReducePartialsOrderInvariant:
    """reduce_partials must produce the same aggregate regardless of the
    order the wave_*.json files are merged in.  This matters because waves
    complete in non-deterministic order on the cluster."""

    def test_reduce_partials_order_invariant(self, tmp_path):
        rng = random.Random(13)
        n_waves = 40
        grid_key = "ridge_1"
        # Write waves in forward order.
        cdir_a = tmp_path / "a" / "_combiner"
        for w in range(n_waves):
            _write_wave(
                cdir_a,
                w,
                {
                    grid_key: {
                        "mse": rng.uniform(1e-4, 1e4),
                        "n_samples": rng.randint(1, 1_000),
                    }
                },
            )

        # Re-use the same content but rename waves so glob ordering differs.
        # We read back the wave payloads, then re-write them under a
        # permuted wave_index -> content mapping into cdir_b.
        payloads = []
        for w in range(n_waves):
            payloads.append(
                json.loads((cdir_a / f"wave_{w}.json").read_text())["grid_points"][grid_key]
            )
        permuted = list(range(n_waves))
        rng.shuffle(permuted)
        cdir_b = tmp_path / "b" / "_combiner"
        for new_idx, src_idx in enumerate(permuted):
            _write_wave(cdir_b, new_idx, {grid_key: payloads[src_idx]})

        a = reduce_partials(cdir_a)
        b = reduce_partials(cdir_b)
        assert a[grid_key]["n_samples"] == b[grid_key]["n_samples"]
        assert abs(a[grid_key]["mse"] - b[grid_key]["mse"]) < 1e-12
