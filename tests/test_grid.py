"""Tests for hpc_mapreduce.job.grid — grid expansion and task manifests."""

from __future__ import annotations

import pytest

from hpc_mapreduce.job.grid import (
    MANIFEST_SCHEMA_VERSION,
    build_task_manifest,
    expand_grid,
    total_tasks,
)


class TestExpandGrid:
    def test_single_dimension(self):
        points = expand_grid({"model": ["a", "b"]})
        assert points == [{"model": "a"}, {"model": "b"}]

    def test_cartesian_product(self):
        points = expand_grid({"x": [1, 2], "y": ["a", "b"]})
        assert len(points) == 4
        assert {"x": "1", "y": "a"} in points
        assert {"x": "2", "y": "b"} in points


class TestBuildTaskManifest:
    def test_grid_only(self):
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01, 0.1]},
            "results/{run_id}",
        )
        assert m["total_tasks"] == 2
        assert "--lr 0.01" in m["tasks"]["0"]["cmd"]
        assert "--lr 0.1" in m["tasks"]["1"]["cmd"]

    def test_result_dir_per_grid_point(self):
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf", "xgb"]},
            "results/{run_id}",
        )
        assert m["tasks"]["0"]["result_dir"] != m["tasks"]["1"]["result_dir"]


class TestTotalTasks:
    def test_simple(self):
        assert total_tasks({"a": [1, 2], "b": [3, 4, 5]}) == 6


class TestBuildTaskManifestMaxTasks:
    def test_raises_when_grid_exceeds_max_tasks(self):
        # 6 total tasks, ceiling of 5 -> ValueError before any tasks are materialized.
        with pytest.raises(ValueError, match=r"max_tasks=5"):
            build_task_manifest(
                "python train.py",
                {"a": [1, 2, 3], "b": [10, 20]},
                "results/{run_id}",
                max_tasks=5,
            )

    def test_disabled_with_none_allows_large_grid(self):
        # 12 total tasks; with max_tasks=None the check is skipped.
        m = build_task_manifest(
            "python train.py",
            {"a": list(range(4)), "b": list(range(3))},
            "results/{run_id}",
            max_tasks=None,
        )
        assert m["total_tasks"] == 12

    def test_raised_threshold_allows_large_grid(self):
        # Same 12 tasks, explicit higher threshold.
        m = build_task_manifest(
            "python train.py",
            {"a": list(range(4)), "b": list(range(3))},
            "results/{run_id}",
            max_tasks=100,
        )
        assert m["total_tasks"] == 12


class TestBuildTaskManifestSchemaVersion:
    def test_schema_version_embedded(self):
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01, 0.1]},
            "results/{run_id}",
        )
        assert m["schema_version"] == MANIFEST_SCHEMA_VERSION
