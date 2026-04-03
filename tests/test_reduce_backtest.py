"""Tests for reduce_backtest — reduction along the backtest time-period axis."""

from __future__ import annotations

import json
from pathlib import Path

from hpc_mapreduce.reduce.metrics import reduce_backtest


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
