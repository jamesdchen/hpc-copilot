"""Tests for reduce_backtest — reduction along the backtest time-period axis."""

from __future__ import annotations

import json
from pathlib import Path

from hpc_mapreduce.reduce.metrics import reduce_backtest, reduce_partials


def _write_metrics(result_dir: Path, metrics: dict) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "metrics.json").write_text(json.dumps(metrics))


class TestReduceBacktest:
    def test_groups_by_grid_point(self, tmp_path):
        """Tasks with same params are grouped; metrics averaged across periods."""
        r1 = tmp_path / "results" / "ridge_1"
        r2 = tmp_path / "results" / "ridge_1b"

        _write_metrics(r1, {"mse": 0.10, "n_samples": 100})
        _write_metrics(r2, {"mse": 0.20, "n_samples": 100})

        manifest = {
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

        result = reduce_backtest(manifest)
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

        manifest = {
            "tasks": {
                "0": {"params": {"model": "ridge"}, "result_dir": str(r1)},
                "1": {"params": {"model": "xgb"}, "result_dir": str(r2)},
            }
        }

        result = reduce_backtest(manifest)
        assert len(result) == 2

    def test_missing_metrics_returns_empty(self, tmp_path):
        """Grid points with no metrics.json get empty dicts."""
        manifest = {
            "tasks": {
                "0": {
                    "params": {"model": "ridge"},
                    "result_dir": str(tmp_path / "nonexistent"),
                },
            }
        }
        result = reduce_backtest(manifest)
        assert len(result) == 1
        assert result[list(result.keys())[0]] == {}

    def test_unequal_period_weights(self, tmp_path):
        """Periods with different n_samples are weighted correctly."""
        r1 = tmp_path / "a"
        r2 = tmp_path / "b"

        _write_metrics(r1, {"mse": 0.10, "n_samples": 100})
        _write_metrics(r2, {"mse": 0.30, "n_samples": 300})

        manifest = {
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

        result = reduce_backtest(manifest)
        key = list(result.keys())[0]
        # Weighted: (0.10*100 + 0.30*300) / 400 = 100/400 = 0.25
        assert abs(result[key]["mse"] - 0.25) < 1e-9
        assert result[key]["n_samples"] == 400


def _write_wave(combiner_dir: Path, wave: int, grid_points: dict) -> None:
    combiner_dir.mkdir(parents=True, exist_ok=True)
    (combiner_dir / f"wave_{wave}.json").write_text(json.dumps({
        "wave": wave,
        "task_ids": [],
        "grid_points": grid_points,
        "errors": [],
    }))


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

    def test_matches_reduce_backtest(self, tmp_path):
        """Cross-validation: reduce_partials and reduce_backtest agree."""
        # Set up result dirs with metrics files
        r1 = tmp_path / "results" / "ridge_a"
        r2 = tmp_path / "results" / "ridge_b"
        _write_metrics(r1, {"mse": 0.10, "n_samples": 100})
        _write_metrics(r2, {"mse": 0.30, "n_samples": 300})

        # reduce_backtest path
        manifest = {
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
        bt_result = reduce_backtest(manifest)

        # reduce_partials path — simulate combiner output for same data
        combiner_dir = tmp_path / "_combiner"
        # Wave 0 has task 0, wave 1 has task 1
        from hpc_mapreduce.job.grid import run_id
        grid_key = run_id({"model": "ridge"})
        _write_wave(combiner_dir, 0, {grid_key: {"mse": 0.10, "n_samples": 100}})
        _write_wave(combiner_dir, 1, {grid_key: {"mse": 0.30, "n_samples": 300}})
        pp_result = reduce_partials(combiner_dir)

        # Both should produce the same aggregated metrics
        assert set(bt_result.keys()) == set(pp_result.keys())
        for key in bt_result:
            for metric in bt_result[key]:
                assert abs(bt_result[key][metric] - pp_result[key][metric]) < 1e-9
