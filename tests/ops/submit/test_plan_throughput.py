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
