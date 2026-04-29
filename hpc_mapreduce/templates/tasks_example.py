"""Canonical example for ``.hpc/tasks.py``.

Every claude-hpc experiment defines a ``tasks.py`` exposing two callables:

    def total() -> int                 # how many tasks
    def resolve(i: int) -> dict        # kwargs for task #i

The framework dispatcher calls ``total()`` once during fan-out and
``resolve(task_id)`` per task. The dict ``resolve()`` returns is merged
into the cluster job's environment as ``HPC_KW_*`` variables and
substituted into the ``result_dir_template`` recorded in the per-run
sidecar.

Eager memoization is the convention: materialize the full task list at
module load. This gives:

* free ``cmd_sha`` (hash of the materialized list at submit time)
* submit-time error catching (a broken ``tasks.py`` fails before qsub)
* deterministic snapshots (every task in the run sees the same ``_TASKS``)
* laptop inspectability (``python -c 'import tasks; print(tasks._TASKS[0])'``)

The agent that scaffolds your ``tasks.py`` will keep whichever pattern
below applies to your experiment and delete the others. Three patterns
are shown inline as commented-out blocks:

  1. Cartesian product over named axes (grid search)
  2. Chunking by row count (e.g. for embarrassingly parallel data work)
  3. Date-window backtests

Pick one, adapt to your axes, delete the rest.
"""

from __future__ import annotations

import itertools

# ---------------------------------------------------------------------------
# Pattern 1: Cartesian product (grid search)
# ---------------------------------------------------------------------------
#
# Use when each task is one cell of a grid over named axes. Two seeds and
# two models below produce four tasks.

_TASKS: list[dict] = [
    {"seed": seed, "model": model}
    for seed, model in itertools.product([42, 1337], ["v1", "v2"])
]

# ---------------------------------------------------------------------------
# Pattern 2: Chunking by row count
# ---------------------------------------------------------------------------
#
# Use when you want to fan out a long-running data job over N row-aligned
# chunks. The user-side executor reads ``HPC_KW_CHUNK_ID`` /
# ``HPC_KW_TOTAL_CHUNKS`` and computes its own slice.
#
# _TOTAL_CHUNKS = 32
# _TASKS = [
#     {"chunk_id": i, "total_chunks": _TOTAL_CHUNKS}
#     for i in range(_TOTAL_CHUNKS)
# ]

# ---------------------------------------------------------------------------
# Pattern 3: Date-window backtests
# ---------------------------------------------------------------------------
#
# Use when each task evaluates one rolling date window. Two-week stride,
# four-week window across one quarter:
#
# from datetime import date, timedelta
#
# _START = date(2026, 1, 1)
# _END = date(2026, 4, 1)
# _WINDOW = timedelta(weeks=4)
# _STRIDE = timedelta(weeks=2)
#
# def _windows():
#     t = _START
#     while t + _WINDOW <= _END:
#         yield {"window_start": t.isoformat(), "window_end": (t + _WINDOW).isoformat()}
#         t += _STRIDE
#
# _TASKS = list(_windows())


def total() -> int:
    return len(_TASKS)


def resolve(task_id: int) -> dict:
    return _TASKS[task_id]
