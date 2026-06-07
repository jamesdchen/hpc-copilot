"""Invariants for the typed task-id coordinate spaces (#299 follow-up).

Pins the ``HpcTaskId`` (0-based domain) ↔ ``ArrayIndex`` (1-based scheduler)
conversion so the ``±1`` rule has one tested home, and so the Phase-2 work
that routes the conversion through the backend membrane has a fixed contract
to move against.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent._kernel.contract.task_id import (
    ArrayIndex,
    HpcTaskId,
    to_array_index,
    to_task_id,
)


def test_boundary_mapping_zero_to_one() -> None:
    """The canonical boundary: HPC_TASK_ID 0 is scheduler array index 1."""
    assert to_array_index(HpcTaskId(0)) == 1
    assert to_task_id(ArrayIndex(1)) == 0


@pytest.mark.parametrize("task_id", [0, 1, 2, 7, 99, 1000])
def test_round_trip_task_id_to_array_and_back(task_id: int) -> None:
    """to_task_id ∘ to_array_index is identity on every valid HpcTaskId."""
    assert to_task_id(to_array_index(HpcTaskId(task_id))) == task_id


@pytest.mark.parametrize("array_index", [1, 2, 3, 8, 100, 1001])
def test_round_trip_array_to_task_id_and_back(array_index: int) -> None:
    """to_array_index ∘ to_task_id is identity on every valid ArrayIndex."""
    assert to_array_index(to_task_id(ArrayIndex(array_index))) == array_index


def test_conversion_is_a_strict_plus_minus_one() -> None:
    """No off-by-something-else: the two spaces differ by exactly 1."""
    for t in range(0, 50):
        assert to_array_index(HpcTaskId(t)) == t + 1
    for a in range(1, 50):
        assert to_task_id(ArrayIndex(a)) == a - 1


def test_negative_task_id_rejected() -> None:
    with pytest.raises(errors.SpecInvalid, match="HpcTaskId must be >= 0"):
        to_array_index(HpcTaskId(-1))


def test_array_index_below_one_rejected() -> None:
    # The scheduler array space is 1-based; 0 / negative is malformed and must
    # NOT silently map to a negative HpcTaskId.
    with pytest.raises(errors.SpecInvalid, match="ArrayIndex must be >= 1"):
        to_task_id(ArrayIndex(0))
    with pytest.raises(errors.SpecInvalid, match="ArrayIndex must be >= 1"):
        to_task_id(ArrayIndex(-3))
