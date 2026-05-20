"""Per-experiment task list — parallelization-planner variant (Pattern 4).

A sibling of ``tasks_example.py`` for *stateful* series experiments — a
walk-forward backtest, an online-learning scan — where the time/series
axis must be partitioned with care. Where ``tasks_example.py``
Patterns 1-3 chop a row range by hand, this one delegates the partition
to :func:`hpc_agent.template.plan_tasks`: the agent classifies the
series axis as a ``DataAxis`` and the planner computes chunk bounds plus
the warm-up halo each chunk must replay.

The contract is unchanged — this file still exposes ``FLAGS`` /
``total()`` / ``resolve()``. Adapt the four TODO blocks below, then
commit it (``.hpc/tasks.py`` is human-owned, hand-editable, and
reviewed like any other source file).

**Mandatory gate.** Before submitting, the parallelization must pass the
serial-elision check — :func:`hpc_agent.template.check_elision` runs the
experiment once whole and once split and asserts they agree. If it
fails, the ``DataAxis`` is misclassified: widen the halo, or fall back
to ``Sequential()``. A misclassified axis produces a job that runs fine
and returns plausible-but-wrong numbers; the gate is the only thing that
catches it.
"""

# ruff: noqa: F401  (all four axis types are imported for the classification choice)

from __future__ import annotations

from hpc_agent.executor_cli import Flag, flag, generic_args
from hpc_agent.template import (
    Associative,
    BoundedHalo,
    Independent,
    Sequential,
    load_series,
    plan_tasks,
    set_series_loader,
)

# ─── 1. FLAGS — per-executor argparse declarations ─────────────────────────
#
# Same contract as tasks_example.py. ``generic_args()`` already ships
# ``--start`` / ``--end``; the planner additionally needs ``--halo``
# (``hpc_agent.template.flags_for_run`` adds it automatically when you
# build FLAGS from a ``@register_run`` signature).

FLAGS: dict[str, list[Flag]] = {
    "src.my_executor": [
        *generic_args(),
        flag("halo", int, default=0),
        # TODO: add the experiment's own flags, e.g. flag("alpha", float, default=1.0)
    ],
}


# ─── 2. Series loader — how the experiment reads its WHOLE series ──────────
#
# ``plan_tasks`` slices on top of this; the executor's ``load_series``
# call then transparently receives just the haloed slice for its chunk.


def _load_whole_series(name: str) -> list[float]:
    """Return the entire ordered series identified by *name*."""
    raise NotImplementedError("TODO: load and return the whole series for `name`")


set_series_loader(_load_whole_series)

_SERIES_NAME = "series"  # TODO: name the series the experiment iterates over
_SERIES_LENGTH = len(load_series(_SERIES_NAME))  # probed once at tasks.py load


# ─── 3. Sweep points — one dict of run kwargs per point ────────────────────

_SWEEP: list[dict] = [{}]  # TODO: e.g. [{"alpha": a} for a in (0.1, 1.0, 10.0)]


# ─── 4. DataAxis classification — the one decision the planner needs ───────
#
# Pick the case that matches the experiment's carried state. On ANY
# uncertainty choose ``Sequential()``: a serial run is slow, never wrong.
#
#   Independent()         — no carried state (a pure per-row map)
#   Associative(monoid)   — carried state, associative transition
#                           (carry a fixed-size monoid summary)
#   BoundedHalo(halo_fn)  — carried state, bounded look-back; halo_fn(params)
#                           returns the warm-up row count — bias it LARGE
#   Sequential()          — unbounded / order-dependent state; not split
#
# See hpc_agent/template/axis.py for the full model.

_DATA_AXIS = Sequential()  # TODO: classify the series axis

_CHUNKS = 16  # chunks per sweep point (ignored for Sequential)

_PLAN = plan_tasks(_SWEEP, _DATA_AXIS, chunks=_CHUNKS, series_length=_SERIES_LENGTH)


def total() -> int:
    return _PLAN.total()


def resolve(task_id: int) -> dict:
    return _PLAN.resolve(task_id)
