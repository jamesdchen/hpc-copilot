"""Tests for the on-cluster combiner script (hpc_mapreduce/map/combiner.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hpc_mapreduce.job.grid import run_id
from hpc_mapreduce.map.combiner import _run_id, _weighted_mean, main


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
