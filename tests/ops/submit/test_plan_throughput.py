"""Tests for the ``plan-throughput`` primitive."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.submit.plan_throughput import plan_throughput

if TYPE_CHECKING:
    from pathlib import Path

_CONSTRAINED = """
    testcluster:
      scheduler: slurm
      constraints:
        max_array_size: 100
        max_concurrent_jobs: 4
        max_walltime: "10:00:00"
        est_spin_up: "1m"
"""


def _clusters_yaml(tmp_path: Path, body: str) -> str:
    path = tmp_path / "clusters.yaml"
    path.write_text(textwrap.dedent(body))
    return str(path)


def test_packs_into_batches_and_waves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _CONSTRAINED))
    out = plan_throughput(cluster="testcluster", total_tasks=250)
    assert out["total_tasks"] == 250
    assert out["total_batches"] == 3  # ceil(250 / 100)
    assert out["max_concurrent"] == 4
    assert out["n_waves"] == 1  # ceil(3 / 4)
    # every task id 0..249 is covered exactly once across the wave_map
    all_ids = [i for ids in out["wave_map"].values() for i in ids]
    assert sorted(all_ids) == list(range(250))


def test_multiple_waves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _CONSTRAINED))
    out = plan_throughput(cluster="testcluster", total_tasks=1000)
    assert out["total_batches"] == 10  # ceil(1000 / 100)
    assert out["n_waves"] == 3  # ceil(10 / 4)


def test_unknown_cluster_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _CONSTRAINED))
    with pytest.raises(errors.ClusterUnknown, match="nope"):
        plan_throughput(cluster="nope", total_tasks=10)


def test_zero_tasks_raises_spec_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _CONSTRAINED))
    with pytest.raises(errors.SpecInvalid):
        plan_throughput(cluster="testcluster", total_tasks=0)


def test_task_exceeding_walltime_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _CONSTRAINED))
    # max_walltime 10h = 36000s; a 40000s task cannot fit.
    with pytest.raises(errors.SpecInvalid, match="walltime"):
        plan_throughput(cluster="testcluster", total_tasks=10, est_task_duration_s=40000)


def test_duration_enables_total_estimate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _CONSTRAINED))
    bare = plan_throughput(cluster="testcluster", total_tasks=10)
    assert bare["est_total_wall_s"] is None
    timed = plan_throughput(cluster="testcluster", total_tasks=10, est_task_duration_s=600)
    assert timed["est_total_wall_s"] is not None
    assert timed["est_total_wall_s"] > 0


def test_cluster_without_constraints_uses_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, "plain:\n  scheduler: sge\n")
    )
    out = plan_throughput(cluster="plain", total_tasks=50)
    # default max_array_size is 1000 -> a 50-task grid is a single array.
    assert out["total_batches"] == 1
    assert out["n_waves"] == 1


# ---------------------------------------------------------------------------
# #345 cost/scale gate — integration through plan_throughput
# ---------------------------------------------------------------------------

# A cluster WITH a cost threshold low enough to trip in tests. 100 tasks x
# 3600s (max_walltime 1h) x 1 core = 100 core-hours; threshold is 10.
_COST_GATED = """
    gated:
      scheduler: slurm
      constraints:
        max_array_size: 1000
        max_concurrent_jobs: 4
        max_walltime: "1:00:00"
        est_spin_up: "1m"
        max_estimated_core_hours: 10
"""


class TestCostGateOffByDefault:
    def test_no_threshold_means_no_cost_gate_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _CONSTRAINED sets no max_estimated_core_hours -> gate is a no-op and
        # the envelope is byte-identical to the pre-#345 shape.
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _CONSTRAINED))
        out = plan_throughput(cluster="testcluster", total_tasks=100_000)
        assert "cost_gate" not in out

    def test_under_threshold_no_cost_gate_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _COST_GATED))
        # 1 task x 3600s x 1 core = 1 core-hour < threshold 10.
        out = plan_throughput(cluster="gated", total_tasks=1)
        assert "cost_gate" not in out


class TestCostGateUnattended:
    def test_over_threshold_refuses_spec_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HPC_AGENT_COST_BUDGET", raising=False)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _COST_GATED))
        # 100 tasks x 3600s x 1 core = 100 core-hours > threshold 10.
        with pytest.raises(errors.SpecInvalid, match="core-hours"):
            plan_throughput(cluster="gated", total_tasks=100)

    def test_over_threshold_message_is_actionable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HPC_AGENT_COST_BUDGET", raising=False)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _COST_GATED))
        with pytest.raises(errors.SpecInvalid) as exc:
            plan_throughput(cluster="gated", total_tasks=100)
        msg = str(exc.value)
        assert "max_estimated_core_hours" in msg
        assert "HPC_AGENT_COST_BUDGET" in msg

    def test_budget_override_allows_over_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _COST_GATED))
        # Estimate is 100 core-hours; threshold 10; operator caps at 500.
        monkeypatch.setenv("HPC_AGENT_COST_BUDGET", "500")
        out = plan_throughput(cluster="gated", total_tasks=100)
        assert out["cost_gate"]["decision"] == "budget_override"
        assert out["cost_gate"]["est_core_hours"] == 100.0

    def test_budget_below_estimate_still_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _COST_GATED))
        # Budget 50 < estimate 100 -> still refused.
        monkeypatch.setenv("HPC_AGENT_COST_BUDGET", "50")
        with pytest.raises(errors.SpecInvalid):
            plan_throughput(cluster="gated", total_tasks=100)


class TestCostGateInteractive:
    def test_over_threshold_returns_confirmation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HPC_AGENT_COST_BUDGET", raising=False)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _COST_GATED))
        out = plan_throughput(cluster="gated", total_tasks=100, interactive=True)
        gate = out["cost_gate"]
        assert gate["decision"] == "requires_confirmation"
        assert gate["est_core_hours"] == 100.0
        assert gate["threshold_core_hours"] == 10.0
        assert "Confirm" in gate["message"]

    def test_cores_per_task_scales_estimate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HPC_AGENT_COST_BUDGET", raising=False)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", _clusters_yaml(tmp_path, _COST_GATED))
        # 1 task x 3600s x 16 cores = 16 core-hours > threshold 10.
        out = plan_throughput(cluster="gated", total_tasks=1, cores_per_task=16, interactive=True)
        assert out["cost_gate"]["est_core_hours"] == 16.0


# ---------------------------------------------------------------------------
# evaluate_cost_gate — direct unit tests (no clusters.yaml round-trip)
# ---------------------------------------------------------------------------


class TestEvaluateCostGateUnit:
    def _est(self, core_hours: float):
        from hpc_agent.infra.cost import CostEstimate

        return CostEstimate(
            total_tasks=10,
            walltime_s=3600,
            cores_per_task=1,
            gpus_per_task=0,
            est_core_hours=core_hours,
            est_gpu_hours=0.0,
        )

    def test_no_threshold_is_noop(self) -> None:
        from hpc_agent.infra.constraints import ClusterConstraints
        from hpc_agent.ops.submit.plan_throughput import evaluate_cost_gate

        c = ClusterConstraints(max_estimated_core_hours=None)
        assert evaluate_cost_gate(c, self._est(1_000_000)) is None

    def test_at_threshold_is_noop(self) -> None:
        from hpc_agent.infra.constraints import ClusterConstraints
        from hpc_agent.ops.submit.plan_throughput import evaluate_cost_gate

        c = ClusterConstraints(max_estimated_core_hours=100.0)
        # Exactly at threshold is allowed (gate is strict-greater).
        assert evaluate_cost_gate(c, self._est(100.0)) is None

    def test_over_threshold_unattended_raises(self) -> None:
        from hpc_agent.infra.constraints import ClusterConstraints
        from hpc_agent.ops.submit.plan_throughput import evaluate_cost_gate

        c = ClusterConstraints(max_estimated_core_hours=100.0)
        with pytest.raises(errors.SpecInvalid):
            evaluate_cost_gate(c, self._est(101.0))

    def test_over_threshold_interactive_confirms(self) -> None:
        from hpc_agent.infra.constraints import ClusterConstraints
        from hpc_agent.ops.submit.plan_throughput import evaluate_cost_gate

        c = ClusterConstraints(max_estimated_core_hours=100.0)
        gate = evaluate_cost_gate(c, self._est(101.0), interactive=True)
        assert gate is not None
        assert gate["decision"] == "requires_confirmation"

    def test_budget_override_when_under_cap(self) -> None:
        from hpc_agent.infra.constraints import ClusterConstraints
        from hpc_agent.ops.submit.plan_throughput import evaluate_cost_gate

        c = ClusterConstraints(max_estimated_core_hours=100.0)
        gate = evaluate_cost_gate(c, self._est(150.0), budget=200.0)
        assert gate is not None
        assert gate["decision"] == "budget_override"
