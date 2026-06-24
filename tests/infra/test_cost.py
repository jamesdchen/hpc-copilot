"""Tests for the #345 compute-cost kernel: estimator + normalization + env budget.

Covers :mod:`hpc_agent.infra.cost` — the single ``footprint → core-hours``
mapping that both the pre-dispatch gate and the post-run actual rollup share.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.cost import (
    CostEstimate,
    core_hours_from_cpu_seconds,
    env_cost_budget,
    estimate_core_hours,
    gpu_hours_from_gpu_seconds,
)

_COST_BUDGET_ENV = "HPC_AGENT_COST_BUDGET"


# ---------------------------------------------------------------------------
# estimate_core_hours
# ---------------------------------------------------------------------------


class TestEstimateCoreHours:
    def test_basic_cpu_estimate(self) -> None:
        # 100 tasks x 3600s x 4 cores = 1_440_000 core-seconds = 400 core-hours
        est = estimate_core_hours(total_tasks=100, walltime_s=3600, cores_per_task=4)
        assert isinstance(est, CostEstimate)
        assert est.est_core_hours == 400.0
        assert est.est_gpu_hours == 0.0
        assert est.cores_per_task == 4
        assert est.gpus_per_task == 0

    def test_gpu_estimate(self) -> None:
        # 10 tasks x 7200s x 2 gpus = 144_000 gpu-seconds = 40 gpu-hours
        est = estimate_core_hours(
            total_tasks=10, walltime_s=7200, cores_per_task=8, gpus_per_task=2
        )
        assert est.est_gpu_hours == 40.0
        # cores: 10 x 7200 x 8 / 3600 = 160 core-hours
        assert est.est_core_hours == 160.0

    def test_cores_default_floor_is_one(self) -> None:
        # Unknown cores -> conservative floor of 1, not 0.
        est = estimate_core_hours(total_tasks=50, walltime_s=3600, cores_per_task=None)
        assert est.cores_per_task == 1
        assert est.est_core_hours == 50.0  # 50 x 3600 x 1 / 3600

    def test_zero_or_negative_cores_floor_to_one(self) -> None:
        est = estimate_core_hours(total_tasks=10, walltime_s=3600, cores_per_task=0)
        assert est.cores_per_task == 1

    def test_zero_tasks_is_zero_cost_not_crash(self) -> None:
        # Pure arithmetic kernel: caller owns rejecting tasks<1; here it's 0.
        est = estimate_core_hours(total_tasks=0, walltime_s=3600, cores_per_task=4)
        assert est.est_core_hours == 0.0

    def test_negative_inputs_clamp(self) -> None:
        est = estimate_core_hours(total_tasks=-5, walltime_s=-10, cores_per_task=4)
        assert est.total_tasks == 0
        assert est.walltime_s == 0
        assert est.est_core_hours == 0.0


# ---------------------------------------------------------------------------
# core_hours_from_cpu_seconds / gpu_hours_from_gpu_seconds
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_cpu_seconds_to_core_hours(self) -> None:
        assert core_hours_from_cpu_seconds(7200.0) == 2.0

    def test_zero_and_negative_clamp(self) -> None:
        assert core_hours_from_cpu_seconds(0) == 0.0
        assert core_hours_from_cpu_seconds(-100) == 0.0

    def test_gpu_seconds_to_gpu_hours(self) -> None:
        assert gpu_hours_from_gpu_seconds(3600.0) == 1.0
        assert gpu_hours_from_gpu_seconds(0) == 0.0

    def test_estimate_matches_actual_normalization(self) -> None:
        # The estimate and the post-run actual must agree on the unit: a job
        # that ran exactly as estimated lands on the same core-hours number.
        tasks, wall, cores = 20, 1800, 3
        est = estimate_core_hours(total_tasks=tasks, walltime_s=wall, cores_per_task=cores)
        # post-run cpu_s totals = tasks x cores x wall (cpu_s already = cores x elapsed_s)
        actual_cpu_s = tasks * cores * wall
        assert core_hours_from_cpu_seconds(actual_cpu_s) == est.est_core_hours


# ---------------------------------------------------------------------------
# env_cost_budget
# ---------------------------------------------------------------------------


class TestEnvCostBudget:
    def test_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_COST_BUDGET_ENV, raising=False)
        assert env_cost_budget() is None

    def test_blank_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_COST_BUDGET_ENV, "   ")
        assert env_cost_budget() is None

    def test_numeric_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_COST_BUDGET_ENV, "5000")
        assert env_cost_budget() == 5000.0

    def test_float_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_COST_BUDGET_ENV, "1234.5")
        assert env_cost_budget() == 1234.5

    def test_negative_is_none_not_infinite_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A fat-fingered negative cap must NOT read as "allow anything".
        monkeypatch.setenv(_COST_BUDGET_ENV, "-1")
        assert env_cost_budget() is None

    def test_garbage_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_COST_BUDGET_ENV, "lots")
        assert env_cost_budget() is None
