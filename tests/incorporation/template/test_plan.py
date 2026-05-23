"""``plan_tasks`` strategy selection per DataAxis (Layer 2)."""

from __future__ import annotations

import pytest

from hpc_agent.incorporation.template import (
    BoundedHalo,
    Independent,
    Sequential,
    plan_tasks,
    sweep_grid,
)


def test_independent_splits_into_contiguous_chunks() -> None:
    plan = plan_tasks([{"alpha": 1.0}, {"alpha": 2.0}], Independent(), chunks=4, series_length=100)
    assert plan.total() == 8  # 2 sweep points * 4 chunks
    bounds = [(plan.resolve(i)["start"], plan.resolve(i)["end"]) for i in range(4)]
    assert bounds == [(0, 25), (25, 50), (50, 75), (75, 100)]
    assert all(plan.resolve(i)["halo"] == 0 for i in range(plan.total()))
    assert plan.resolve(0)["alpha"] == 1.0


def test_sequential_emits_one_task_per_sweep_point() -> None:
    plan = plan_tasks([{"a": 1}, {"a": 2}], Sequential(), chunks=16, series_length=100)
    assert plan.total() == 2
    assert plan.resolve(0) == {"a": 1, "start": 0, "end": 100, "halo": 0}


def test_bounded_halo_clamps_first_chunk_and_widens_the_rest() -> None:
    plan = plan_tasks([{"w": 12}], BoundedHalo(lambda p: p["w"]), chunks=4, series_length=100)
    halos = [plan.resolve(i)["halo"] for i in range(4)]
    # chunk 0 starts at row 0 -> nothing to replay; the rest get the full halo.
    assert halos == [0, 12, 12, 12]


def test_chunks_clamped_so_no_empty_chunk() -> None:
    plan = plan_tasks([{"a": 1}], Independent(), chunks=10, series_length=3)
    assert plan.total() == 3


def test_empty_sweep_raises() -> None:
    with pytest.raises(ValueError, match="at least one sweep point"):
        plan_tasks([], Independent(), chunks=2, series_length=10)


def test_sweep_grid_builds_cartesian_product() -> None:
    grid = sweep_grid(alpha=[0.1, 1.0], horizon=[1, 5])
    assert len(grid) == 4
    assert {"alpha": 0.1, "horizon": 1} in grid
    assert {"alpha": 1.0, "horizon": 5} in grid
