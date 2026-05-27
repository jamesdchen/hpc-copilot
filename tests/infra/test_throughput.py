"""Tests for the throughput optimizer."""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra.constraints import ClusterConstraints
from hpc_agent.infra.throughput import (
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
            est_spin_up="5m",  # 300s
        )
        # 400s task + 300s spin-up = 700s > 600s limit
        workload = WorkloadSpec(total_tasks=50, est_task_duration_s=400)
        with pytest.raises(errors.SpecInvalid, match="exceeds max walltime"):
            compute_submission_plan(constraints, workload)


class TestInputValidationBoundaries:
    """Surfaced by mutmut on compute_submission_plan — the input
    validation guards (``total_tasks``, ``max_array_size``,
    ``max_concurrent_jobs`` all >= 1) had no exact-boundary tests, so
    every off-by-one mutation on those checks survived. These tests
    pin the boundary semantics."""

    def test_max_concurrent_jobs_zero_rejected(self):
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=0)
        with pytest.raises(errors.SpecInvalid, match="max_concurrent_jobs must be >= 1"):
            compute_submission_plan(constraints, WorkloadSpec(total_tasks=10))

    def test_max_concurrent_jobs_one_accepted(self):
        """The smallest accepted value — pins ``<= 0`` vs ``< 0``."""
        constraints = ClusterConstraints(max_array_size=100, max_concurrent_jobs=1)
        plan = compute_submission_plan(constraints, WorkloadSpec(total_tasks=10))
        assert plan.max_concurrent == 1

    def test_max_array_size_zero_rejected(self):
        constraints = ClusterConstraints(max_array_size=0)
        with pytest.raises(errors.SpecInvalid, match="max_array_size must be >= 1"):
            compute_submission_plan(constraints, WorkloadSpec(total_tasks=10))

    def test_max_array_size_one_accepted(self):
        """The smallest accepted value — pins ``<= 0`` vs ``<= 1``."""
        constraints = ClusterConstraints(max_array_size=1, max_concurrent_jobs=10)
        plan = compute_submission_plan(constraints, WorkloadSpec(total_tasks=3))
        assert plan.total_batches == 3
        assert all(b.array_size == 1 for b in plan.batches)


class TestWalltimeBoundaries:
    """Surfaced by mutmut on compute_submission_plan's walltime check.
    The guard is ``if walltime_limit > 0 and effective_time >
    walltime_limit`` — three boundaries: walltime_limit=0 (skip
    check), walltime_limit=1 (still active), effective_time exactly
    at walltime_limit (no raise)."""

    def test_walltime_limit_zero_skips_check(self):
        """When ``walltime_limit`` parses to 0 (unset / unparseable),
        the check is intentionally skipped. Tasks taking arbitrary
        time are still planned without raising."""
        # parse_walltime_to_sec returns 0 for invalid strings; the
        # comment in compute_submission_plan documents this skip path.
        constraints = ClusterConstraints(
            max_array_size=100,
            max_walltime="garbage_that_parses_to_zero",
            est_spin_up="0s",
        )
        assert constraints.walltime_seconds() == 0
        plan = compute_submission_plan(
            constraints,
            WorkloadSpec(total_tasks=1, est_task_duration_s=1_000_000),
        )
        # No raise — the check is skipped, plan is built.
        assert plan.total_batches == 1

    def test_walltime_exact_match_does_not_raise(self):
        """``effective_time == walltime_limit`` is the documented
        boundary: pass (strict ``>`` in the raise condition). Pins the
        boundary so a future flip to ``>=`` is caught."""
        constraints = ClusterConstraints(
            max_array_size=100,
            max_walltime="0:10:00",  # 600s
            est_spin_up="5m",  # 300s
        )
        # 300s task + 300s spin-up = exactly 600s
        workload = WorkloadSpec(total_tasks=10, est_task_duration_s=300)
        plan = compute_submission_plan(constraints, workload)
        # Should not raise; plan is built.
        assert plan.total_batches == 1

    def test_walltime_one_second_over_raises(self):
        """One second past the limit raises — the ``>`` boundary."""
        constraints = ClusterConstraints(
            max_array_size=100,
            max_walltime="0:10:00",  # 600s
            est_spin_up="5m",  # 300s
        )
        workload = WorkloadSpec(total_tasks=10, est_task_duration_s=301)
        with pytest.raises(errors.SpecInvalid, match="exceeds max walltime"):
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

    def test_per_batch_est_wall_s_populated_when_duration_known(self):
        """Regression: each ``JobBatch.est_wall_s`` carries the per-batch
        wall-time estimate when ``est_task_duration_s`` is provided.
        Surfaced by mutmut — the existing tests only asserted on the
        all-None branch (TestUnknownDuration.test_batch_wall_is_none),
        so a mutation that set ``est_wall_s=None`` for the
        duration-known path slipped through and the
        ``plan_throughput`` envelope's per-batch ETA could silently
        flip to null."""
        constraints = ClusterConstraints(
            max_array_size=100,
            max_concurrent_jobs=1,
            est_spin_up="5m",  # 300s
        )
        workload = WorkloadSpec(total_tasks=50, est_task_duration_s=600)
        plan = compute_submission_plan(constraints, workload)
        # Spin-up + duration = 300 + 600 = 900s, threaded into every batch.
        for b in plan.batches:
            assert b.est_wall_s == 900


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
