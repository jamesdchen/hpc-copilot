"""``plan_tasks`` — turn a sweep + a :data:`DataAxis` into a task list.

The output :class:`TaskPlan` exposes ``total()`` and ``resolve(task_id)``
— exactly the contract ``.hpc/tasks.py`` must satisfy. A generated
``tasks.py`` is a thin wrapper:

.. code-block:: python

    from hpc_agent.incorporation.template import plan_tasks, BoundedHalo

    _PLAN = plan_tasks(
        sweep=[{"alpha": a} for a in (0.1, 1.0, 10.0)],
        data_axis=BoundedHalo(lambda p: 48 * 30),
        chunks=16,
        series_length=8760,
    )

    def total() -> int:
        return _PLAN.total()

    def resolve(task_id: int) -> dict:
        return _PLAN.resolve(task_id)

Each resolved task dict carries the sweep point's params plus the
slice keys ``start`` / ``end`` / ``halo`` that
:func:`hpc_agent.incorporation.template.load_series` consumes.

Stdlib-only — safe to import at dispatch time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from hpc_agent.incorporation.template.axis import Associative, BoundedHalo, DataAxis, Independent, Sequential

__all__ = ["TaskPlan", "plan_tasks", "sweep_grid"]


@dataclass(frozen=True)
class TaskPlan:
    """A materialised task list — the ``total()`` / ``resolve()`` contract.

    ``axis_kind`` records which :data:`DataAxis` produced the plan so
    the reduce phase knows whether to concatenate, monoid-fold, or trim
    halos.
    """

    tasks: tuple[dict[str, Any], ...]
    axis_kind: str
    n_sweep_points: int
    n_chunks: int

    def total(self) -> int:
        return len(self.tasks)

    def resolve(self, task_id: int) -> dict[str, Any]:
        return dict(self.tasks[task_id])


def sweep_grid(**axes: Iterable[Any]) -> list[dict[str, Any]]:
    """Cartesian product of named axes → a list of sweep-point dicts.

    ``sweep_grid(alpha=[0.1, 1.0], horizon=[1, 5])`` → four dicts. A
    convenience for the common grid-search ``sweep`` argument to
    :func:`plan_tasks`.
    """
    import itertools

    names = list(axes)
    value_lists = [list(axes[n]) for n in names]
    return [dict(zip(names, combo, strict=False)) for combo in itertools.product(*value_lists)]


def plan_tasks(
    sweep: Iterable[dict[str, Any]],
    data_axis: DataAxis,
    *,
    chunks: int = 1,
    series_length: int,
) -> TaskPlan:
    """Apply the strategy for *data_axis* and return a :class:`TaskPlan`.

    Parameters
    ----------
    sweep:
        The sweep points — one ``dict`` of run kwargs per point. Use
        :func:`sweep_grid` for a Cartesian grid.
    data_axis:
        The :data:`DataAxis` classification of the series axis.
    chunks:
        Desired number of chunks the series is split into *per sweep
        point*. Ignored for :class:`Sequential` (always one chunk).
        Clamped down to ``series_length`` so no empty chunk is emitted.
    series_length:
        Length of the totally-ordered series being partitioned.

    Returns
    -------
    A :class:`TaskPlan`; ``total()`` is ``len(sweep) * chunks_used``.
    """
    points = [dict(p) for p in sweep]
    if not points:
        raise ValueError("plan_tasks requires at least one sweep point")
    if series_length < 0:
        raise ValueError(f"series_length must be non-negative; got {series_length}")

    sequential = isinstance(data_axis, Sequential)
    if sequential:
        ranges: list[tuple[int, int]] = [(0, series_length)]
    else:
        ranges = _contiguous_ranges(series_length, chunks)

    tasks: list[dict[str, Any]] = []
    for point in points:
        for start, end in ranges:
            halo = _halo_for(data_axis, point, start)
            tasks.append({**point, "start": start, "end": end, "halo": halo})

    return TaskPlan(
        tasks=tuple(tasks),
        axis_kind=type(data_axis).__name__,
        n_sweep_points=len(points),
        n_chunks=len(ranges),
    )


def _halo_for(data_axis: DataAxis, point: dict[str, Any], start: int) -> int:
    """Halo width for one chunk, clamped so it never reaches before row 0."""
    if isinstance(data_axis, BoundedHalo):
        requested = int(data_axis.halo_fn(point))
        return max(0, min(requested, start))
    # Independent / Associative / Sequential carry no halo.
    if isinstance(data_axis, (Independent, Associative, Sequential)):
        return 0
    raise TypeError(f"unknown DataAxis type: {type(data_axis).__name__}")


def _contiguous_ranges(n: int, k: int) -> list[tuple[int, int]]:
    """Split ``[0, n)`` into at most *k* contiguous near-equal ranges.

    ``k`` is clamped to ``[1, n]`` so no empty range is produced (an
    empty chunk would be a task that does nothing). The first
    ``n % k`` ranges are one element longer.
    """
    if n <= 0:
        return [(0, 0)]
    k = max(1, min(int(k), n))
    base, extra = divmod(n, k)
    ranges: list[tuple[int, int]] = []
    pos = 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        ranges.append((pos, pos + size))
        pos += size
    return ranges
