"""Canonical example for ``.hpc/tasks.py``.

Every claude-hpc experiment defines a ``tasks.py`` exposing three things:

    FLAGS: dict[str, list[Flag]]    # per-executor CLI flag specs
    def total() -> int               # how many tasks
    def resolve(i: int) -> dict      # kwargs for task #i

``FLAGS`` is keyed by importable executor module path (e.g.
``"src.ml_ridge"``) and lists the flags that executor's argparse will
accept. Each ``/submit-hpc`` invocation picks one key; the
``.hpc/cli.py`` dispatcher uses the matching list to build the parser
at runtime. Methods that don't share flags don't bleed into each
other's parsers.

``total()`` reports the task count for the chosen executor's
fan-out grid; ``resolve(task_id)`` returns the kwargs for one task.
The framework dispatcher merges those kwargs into the cluster job's
environment as ``HPC_KW_*`` variables and substitutes them into
``result_dir_template`` recorded in the per-run sidecar.

Eager memoization is the convention: materialize the full task list
at module load. This gives:

* free ``cmd_sha`` (hash of the materialized list at submit time)
* submit-time error catching (a broken ``tasks.py`` fails before qsub)
* deterministic snapshots (every task in the run sees the same ``_TASKS``)
* laptop inspectability (``python -c 'import tasks; print(tasks._TASKS[0])'``)

The agent that scaffolds your ``tasks.py`` will keep whichever pattern
below applies to your experiment and delete the others.
"""

from __future__ import annotations

import itertools

from claude_hpc.executor_cli import flag, generic_args, gpu_args

# ─── FLAGS: per-executor argparse declarations ─────────────────────────────
#
# Keys are importable module paths. Values are lists of Flag specs
# (use ``flag()`` for ad-hoc, ``generic_args()`` / ``gpu_args()`` for
# common bundles). The dispatcher errors fast on unknown keys, so a
# typo here surfaces immediately rather than as a confusing argparse
# failure on the cluster.
#
# Keep one entry per executor in the repo so /submit-hpc can pick any
# of them at run time without re-editing tasks.py.

FLAGS: dict[str, list] = {
    "src.ml_ridge": [
        *generic_args(),
        flag("horizon", int, default=1),
        flag("alpha", float, default=1.0),
    ],
    "src.dl_patchts": [
        *generic_args(),
        *gpu_args(),
        flag("horizon", int, default=1),
    ],
}

# ─── Pattern 1: Cartesian product (grid search) ────────────────────────────
#
# Use when each task is one cell of a grid over named axes. Two seeds
# and two horizons below produce four tasks.

_TASKS: list[dict] = [{"horizon": h, "seed": s} for h, s in itertools.product([1, 5], [42, 1337])]

# ─── Pattern 2: Chunking by row count ──────────────────────────────────────
#
# Use when you want to fan out a long-running data job over N row-aligned
# chunks. The user-side executor reads ``args.chunk_id`` /
# ``args.total_chunks`` and computes its own slice.
#
# _TOTAL_CHUNKS = 32
# _TASKS = [
#     {"chunk_id": i, "total_chunks": _TOTAL_CHUNKS}
#     for i in range(_TOTAL_CHUNKS)
# ]

# ─── Pattern 3: Date-window backtests ──────────────────────────────────────
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
#         yield {"start": t.toordinal(), "end": (t + _WINDOW).toordinal()}
#         t += _STRIDE
#
# _TASKS = list(_windows())


def total() -> int:
    return len(_TASKS)


def resolve(task_id: int) -> dict:
    return _TASKS[task_id]
