"""Tests for the throughput optimizer."""

from __future__ import annotations

import pytest

from hpc_mapreduce.job.constraints import ClusterConstraints
from hpc_mapreduce.job.throughput import (
    WorkloadSpec,
    build_wave_map,
    compute_submission_plan,
)


class TestExactFit:
    """200 tasks, max_array=100, max_concurrent=2 → 2 batches, 1 wave."""

    def test_batch_count(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=200)
        plan = compute_submission_plan(constraints, workload)
        assert plan.total_batches == 2

    def test_wave_count(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=200)
        plan = compute_submission_plan(constraints, workload)
        assert len({b.wave for b in plan.batches}) == 1

    def test_both_immediate(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=200)
        plan = compute_submission_plan(constraints, workload)
        assert all(b.wave == 0 for b in plan.batches)


class TestMultipleWaves:
    """350 tasks, max_array=100, max_concurrent=2 → 4 batches, 2 waves."""

    @pytest.fixture()
    def plan(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=350)
        return compute_submission_plan(constraints, workload)

    def test_batch_count(self, plan):
        assert plan.total_batches == 4

    def test_wave_count(self, plan):
        n_waves = max(b.wave for b in plan.batches) + 1
        assert n_waves == 2

    def test_task_ranges(self, plan):
        ranges = [b.task_range for b in plan.batches]
        assert ranges == ["1-88", "89-176", "177-264", "265-350"]

    def test_wave_assignments(self, plan):
        wave_0 = [b.batch_index for b in plan.batches if b.wave == 0]
        wave_1 = [b.batch_index for b in plan.batches if b.wave == 1]
        assert wave_0 == [0, 1]
        assert wave_1 == [2, 3]


class TestSingleBatch:
    """50 tasks, max_array=100 → 1 batch, 1 wave."""

    def test_single_batch(self):
        constraints = ClusterConstraints(max_array_size=100)
        workload = WorkloadSpec(total_tasks=50)
        plan = compute_submission_plan(constraints, workload)
        assert plan.total_batches == 1
        assert len(plan.batches) == 1
        assert plan.batches[0].wave == 0
        assert plan.batches[0].task_range == "1-50"


class TestUnknownDuration:
    """est_task_duration_s=None → plan computed, estimates are None."""

    def test_plan_computed(self):
        constraints = ClusterConstraints(max_array_size=100)
        workload = WorkloadSpec(total_tasks=200, est_task_duration_s=None)
        plan = compute_submission_plan(constraints, workload)
        assert plan.total_batches == 2

    def test_total_wall_is_none(self):
        constraints = ClusterConstraints(max_array_size=100)
        workload = WorkloadSpec(total_tasks=200, est_task_duration_s=None)
        plan = compute_submission_plan(constraints, workload)
        assert plan.est_total_wall_s is None

    def test_batch_wall_is_none(self):
        constraints = ClusterConstraints(max_array_size=100)
        workload = WorkloadSpec(total_tasks=200, est_task_duration_s=None)
        plan = compute_submission_plan(constraints, workload)
        assert all(b.est_wall_s is None for b in plan.batches)


class TestWalltimeExceeded:
    """task duration + spin-up > max_walltime → raises ValueError."""

    def test_raises(self):
        constraints = ClusterConstraints(
            max_array_size=100,
            max_walltime="0:10:00",  # 600s
            est_spin_up="5m",        # 300s
        )
        # 400s task + 300s spin-up = 700s > 600s limit
        workload = WorkloadSpec(total_tasks=50, est_task_duration_s=400)
        with pytest.raises(ValueError, match="exceeds max walltime"):
            compute_submission_plan(constraints, workload)


class TestEvenDistribution:
    """350 tasks in 4 batches → no batch smaller than 86."""

    def test_no_tiny_last_batch(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=350)
        plan = compute_submission_plan(constraints, workload)
        sizes = [b.array_size for b in plan.batches]
        assert min(sizes) >= 86

    def test_total_tasks_match(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=350)
        plan = compute_submission_plan(constraints, workload)
        assert sum(b.array_size for b in plan.batches) == 350


class TestTimeEstimation:
    """100 tasks, max_array=100, max_concurrent=1, est_task=600s, spin_up=300s."""

    def test_one_wave(self):
        constraints = ClusterConstraints(
            max_array_size=100,
            max_concurrent_jobs=1,
            est_spin_up="5m",
        )
        workload = WorkloadSpec(total_tasks=100, est_task_duration_s=600)
        plan = compute_submission_plan(constraints, workload)
        assert max(b.wave for b in plan.batches) + 1 == 1

    def test_estimated_total(self):
        constraints = ClusterConstraints(
            max_array_size=100,
            max_concurrent_jobs=1,
            est_spin_up="5m",
        )
        workload = WorkloadSpec(total_tasks=100, est_task_duration_s=600)
        plan = compute_submission_plan(constraints, workload)
        # 1 wave * (600 + 300) = 900
        assert plan.est_total_wall_s == 900


class TestStrategyString:
    """Strategy string contains batch count, concurrent count, wave count."""

    def test_contains_info(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=350)
        plan = compute_submission_plan(constraints, workload)
        assert "4 batches" in plan.strategy
        assert "2 concurrent" in plan.strategy
        assert "2 waves" in plan.strategy


class TestTaskRangeProperty:
    """JobBatch.task_range returns '1-100' format string."""

    def test_task_range_format(self):
        constraints = ClusterConstraints(max_array_size=100)
        workload = WorkloadSpec(total_tasks=100)
        plan = compute_submission_plan(constraints, workload)
        assert plan.batches[0].task_range == "1-100"


class TestBuildWaveMapSingleWave:
    """All batches in wave 0 → all task IDs in wave 0."""

    def test_all_tasks_in_wave_zero(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=200)
        plan = compute_submission_plan(constraints, workload)
        wave_map = build_wave_map(plan)
        assert set(wave_map.keys()) == {0}
        assert wave_map[0] == list(range(200))

    def test_task_ids_are_zero_based(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=200)
        plan = compute_submission_plan(constraints, workload)
        wave_map = build_wave_map(plan)
        assert min(wave_map[0]) == 0
        assert max(wave_map[0]) == 199


class TestBuildWaveMapMultiWave:
    """350 tasks, max_array=100, max_concurrent=2 → 2 waves."""

    def test_correct_wave_keys(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=350)
        plan = compute_submission_plan(constraints, workload)
        wave_map = build_wave_map(plan)
        assert set(wave_map.keys()) == {0, 1}

    def test_no_overlap(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=350)
        plan = compute_submission_plan(constraints, workload)
        wave_map = build_wave_map(plan)
        ids_0 = set(wave_map[0])
        ids_1 = set(wave_map[1])
        assert ids_0.isdisjoint(ids_1)

    def test_all_tasks_covered(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=2)
        workload = WorkloadSpec(total_tasks=350)
        plan = compute_submission_plan(constraints, workload)
        wave_map = build_wave_map(plan)
        all_ids = sorted(wave_map[0] + wave_map[1])
        assert all_ids == list(range(350))


class TestBuildWaveMapSingleBatch:
    """Single batch → wave 0 contains all tasks."""

    def test_single_batch_wave_zero(self):
        constraints = ClusterConstraints(max_array_size=100)
        workload = WorkloadSpec(total_tasks=50)
        plan = compute_submission_plan(constraints, workload)
        wave_map = build_wave_map(plan)
        assert set(wave_map.keys()) == {0}
        assert wave_map[0] == list(range(50))
