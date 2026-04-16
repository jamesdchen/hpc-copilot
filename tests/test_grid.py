"""Tests for hpc_mapreduce.job.grid — grid expansion and task manifests."""

from __future__ import annotations

from hpc_mapreduce.job.grid import (
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
    def test_no_chunking(self):
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01, 0.1]},
            "results/{run_id}",
        )
        assert m["total_tasks"] == 2
        assert m["chunks_per_point"] == 1
        assert "--lr 0.01" in m["tasks"]["0"]["cmd"]
        assert "--lr 0.1" in m["tasks"]["1"]["cmd"]

    def test_with_chunking_defaults(self):
        m = build_task_manifest(
            "python train.py",
            {"model": ["ridge"]},
            "results/{run_id}",
            chunking={"total": 3},
        )
        assert m["total_tasks"] == 3
        assert "--chunk-id 0 --total-chunks 3" in m["tasks"]["0"]["cmd"]
        assert "--chunk-id 2 --total-chunks 3" in m["tasks"]["2"]["cmd"]
        assert m["tasks"]["0"]["chunk_id"] == 0
        assert m["tasks"]["2"]["chunk_id"] == 2

    def test_custom_chunk_args(self):
        m = build_task_manifest(
            "python train.py",
            {"model": ["xgb"]},
            "results/{run_id}",
            chunking={"total": 2, "chunk_arg": "--shard", "total_arg": "--num-shards"},
        )
        assert "--shard 0 --num-shards 2" in m["tasks"]["0"]["cmd"]
        assert "--shard 1 --num-shards 2" in m["tasks"]["1"]["cmd"]

    def test_grid_times_chunks(self):
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf", "xgb"]},
            "results/{run_id}",
            chunking={"total": 3},
        )
        assert m["total_tasks"] == 6  # 2 models × 3 chunks
        assert m["grid_size"] == 2
        assert "--model rf" in m["tasks"]["0"]["cmd"]
        assert m["tasks"]["0"]["chunk_id"] == 0
        assert "--model xgb" in m["tasks"]["3"]["cmd"]
        assert m["tasks"]["3"]["chunk_id"] == 0

    def test_result_dir_per_grid_point(self):
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf", "xgb"]},
            "results/{run_id}",
            chunking={"total": 2},
        )
        # All chunks of same grid point share a result dir
        assert m["tasks"]["0"]["result_dir"] == m["tasks"]["1"]["result_dir"]
        # Different grid points have different result dirs
        assert m["tasks"]["0"]["result_dir"] != m["tasks"]["2"]["result_dir"]


class TestTotalTasks:
    def test_simple(self):
        assert total_tasks({"a": [1, 2], "b": [3, 4, 5]}) == 6

    def test_with_chunks(self):
        assert total_tasks({"a": [1, 2]}, chunks=10) == 20
