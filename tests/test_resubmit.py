"""Tests for hpc_mapreduce.job.resubmit — compact ID packing and plan building."""

from __future__ import annotations

import pytest

from hpc_mapreduce.job.constraints import ClusterConstraints
from hpc_mapreduce.job.resubmit import (
    ResubmitBatch,
    ResubmitPlan,
    compact_task_ids,
    resubmit_plan,
)


def _fake_manifest(n_tasks: int) -> dict:
    """Build a minimal manifest of *n_tasks* tasks for resubmit tests."""
    return {
        "schema_version": 1,
        "total_tasks": n_tasks,
        "tasks": {
            str(i): {
                "cmd": f"echo task {i}",
                "result_dir": f"/tmp/results/task_{i}",
            }
            for i in range(1, n_tasks + 1)
        },
    }


class TestCompactTaskIds:
    def test_compact_task_ids_contiguous(self):
        assert compact_task_ids([1, 2, 3]) == "1-3"

    def test_compact_task_ids_mixed(self):
        assert compact_task_ids([3, 7, 12, 13, 14]) == "3,7,12-14"

    def test_compact_task_ids_single(self):
        assert compact_task_ids([5]) == "5"

    def test_compact_task_ids_empty(self):
        with pytest.raises(ValueError):
            compact_task_ids([])

    def test_compact_task_ids_unsorted_input(self):
        # Sorts defensively before compacting.
        assert compact_task_ids([14, 3, 13, 7, 12]) == "3,7,12-14"


class TestResubmitPlanBasic:
    def test_resubmit_plan_basic(self):
        manifest = _fake_manifest(60)
        failed = [3, 7, 12, 13, 14]
        plan = resubmit_plan(manifest, failed)

        assert isinstance(plan, ResubmitPlan)
        assert plan.total_tasks == 5
        assert plan.total_batches == 1
        assert len(plan.batches) == 1

        # Single wave (all batches have wave == 0).
        assert {b.wave for b in plan.batches} == {0}

        # task_range encodes the failed IDs compactly.
        only = plan.batches[0]
        assert isinstance(only, ResubmitBatch)
        assert only.task_range == "3,7,12-14"
        assert only.array_size == 5
        assert only.task_ids == (3, 7, 12, 13, 14)

    def test_resubmit_plan_overrides_attached(self):
        manifest = _fake_manifest(10)
        plan = resubmit_plan(
            manifest,
            [1, 2],
            overrides={"mem": "32G", "walltime": "12:00:00"},
        )
        assert plan.overrides == {"mem": "32G", "walltime": "12:00:00"}

    def test_resubmit_plan_no_overrides_yields_empty_dict(self):
        manifest = _fake_manifest(10)
        plan = resubmit_plan(manifest, [1])
        assert plan.overrides == {}


class TestResubmitPlanValidation:
    def test_resubmit_plan_rejects_unknown_id(self):
        manifest = _fake_manifest(60)
        with pytest.raises(ValueError, match="not present in manifest"):
            resubmit_plan(manifest, [999])

    def test_resubmit_plan_rejects_empty(self):
        manifest = _fake_manifest(60)
        with pytest.raises(ValueError):
            resubmit_plan(manifest, [])


class TestResubmitPlanBatching:
    def test_resubmit_plan_splits_over_max_array(self):
        """5 failed IDs with max_array_size=3 must fan out to >=2 batches."""
        manifest = _fake_manifest(60)
        failed = [2, 5, 9, 20, 42]
        constraints = ClusterConstraints(max_array_size=3, max_concurrent_jobs=10)

        plan = resubmit_plan(manifest, failed, constraints=constraints)

        assert plan.total_batches >= 2
        assert len(plan.batches) >= 2

        # Union of task_ids across batches == the original failed list (sorted).
        all_ids: list[int] = []
        for b in plan.batches:
            assert b.array_size <= 3
            all_ids.extend(b.task_ids)
        assert sorted(all_ids) == sorted(failed)

        # Each batch's task_ids are within the known manifest.
        for b in plan.batches:
            for tid in b.task_ids:
                assert str(tid) in manifest["tasks"]
