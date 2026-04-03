"""Tests for grid expansion and task manifest generation."""

from __future__ import annotations

import pytest

from hpc_mapreduce.job.grid import attach_wave_map, build_task_manifest, expand_grid, total_tasks


class TestExpandGrid:
    def test_basic_expansion(self):
        grid = {"a": [1, 2], "b": ["x", "y"]}
        points = expand_grid(grid)
        assert len(points) == 4
        # All values should be strings
        for p in points:
            assert all(isinstance(v, str) for v in p.values())

    def test_single_param(self):
        grid = {"lr": [0.01, 0.1, 1.0]}
        points = expand_grid(grid)
        assert len(points) == 3


class TestBuildTaskManifest:
    def test_basic_grid_no_backtest(self):
        grid = {"x": [1, 2], "y": ["a", "b"]}
        manifest = build_task_manifest(
            run_cmd="python run.py",
            grid=grid,
            result_dir_template="/out/{run_id}",
        )
        assert manifest["total_tasks"] == 4
        assert manifest["grid_size"] == 4
        assert len(manifest["tasks"]) == 4
        assert "grid_keys" in manifest
        assert manifest["grid_keys"] == ["x", "y"]

    def test_no_chunk_id_in_tasks(self):
        """Verify no chunk_id key exists in task entries."""
        grid = {"a": [1]}
        manifest = build_task_manifest(
            run_cmd="echo hello",
            grid=grid,
            result_dir_template="/out/{run_id}",
        )
        for task in manifest["tasks"].values():
            assert "chunk_id" not in task

    def test_total_tasks_matches_grid_size(self):
        grid = {"x": [1, 2, 3], "y": ["a", "b"]}
        manifest = build_task_manifest(
            run_cmd="python run.py",
            grid=grid,
            result_dir_template="/out/{run_id}",
        )
        assert manifest["total_tasks"] == 6
        assert len(manifest["tasks"]) == 6

    def test_task_keys_are_sequential_strings(self):
        grid = {"x": [1, 2, 3]}
        manifest = build_task_manifest(
            run_cmd="python run.py",
            grid=grid,
            result_dir_template="/out/{run_id}",
        )
        assert set(manifest["tasks"].keys()) == {"0", "1", "2"}

    def test_task_has_cmd_and_result_dir(self):
        grid = {"lr": [0.01]}
        manifest = build_task_manifest(
            run_cmd="python train.py",
            grid=grid,
            result_dir_template="/results/{run_id}",
        )
        task = manifest["tasks"]["0"]
        assert "cmd" in task
        assert "result_dir" in task
        assert "python train.py" in task["cmd"]
        assert "--lr 0.01" in task["cmd"]


class TestTotalTasks:
    def test_grid_only(self):
        grid = {"a": [1, 2], "b": [3, 4, 5]}
        assert total_tasks(grid) == 6

    def test_grid_with_backtest(self):
        grid = {"a": [1, 2]}
        backtest = {
            "start": "2020-01-01",
            "end": "2022-12-31",
            "chunk_duration": "1Y",
        }
        assert total_tasks(grid, backtest=backtest) == 6

    def test_single_param_single_value(self):
        grid = {"x": [42]}
        assert total_tasks(grid) == 1


class TestAttachWaveMap:
    def test_basic(self):
        manifest = {"tasks": {"0": {}, "1": {}}, "total_tasks": 2}
        wave_map = {0: [0, 1]}
        result = attach_wave_map(manifest, wave_map)
        assert "wave_map" in result
        # Keys should be strings
        assert set(result["wave_map"].keys()) == {"0"}
        # Task IDs should be strings
        assert result["wave_map"]["0"] == ["0", "1"]

    def test_preserves_manifest(self):
        manifest = {"tasks": {"0": {}, "1": {}}, "total_tasks": 2}
        wave_map = {0: [0, 1]}
        result = attach_wave_map(manifest, wave_map)
        # Original should not be mutated
        assert "wave_map" not in manifest
        # Original keys preserved in result
        assert result["tasks"] == manifest["tasks"]
        assert result["total_tasks"] == 2
