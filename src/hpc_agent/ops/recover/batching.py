# @pure: no-io
"""Resubmission helpers: re-run only the failed task IDs from a prior job.

When a grid job finishes with some failed tasks, ``/status`` (and the LLM)
needs to resubmit exactly those task IDs — possibly under adjusted resources
(more memory, a longer walltime) — without duplicating the on-cluster
batching logic that lives in :mod:`hpc_agent.ops.submit.throughput`.

This module provides:

* :func:`compact_task_ids` — pack a sorted list of task IDs into an
  ``sbatch``/``qsub`` array expression (``"3,7,12-14"`` etc.).
* :class:`ResubmitPlan` / :class:`ResubmitBatch` — the resubmission
  analogue of :class:`~hpc_agent.ops.submit.throughput.SubmissionPlan`, whose
  ``task_range`` strings can be *non-contiguous* since the failed IDs are
  arbitrary.
* :func:`resubmit_plan` — build a :class:`ResubmitPlan` from a known task
  count and a list of failed task IDs, reusing
  :func:`~hpc_agent.ops.submit.throughput.compute_submission_plan` to split the
  failures into batches that honour the cluster's ``max_array_size`` and
  ``max_concurrent_jobs`` limits.

The framework executor still resolves per-task kwargs at runtime via
``tasks.resolve(task_id)``, so resubmit doesn't need to know what the
tasks actually are — only how many exist and which ones to retry.

Only stdlib imports — keeps the library dep-free.
"""

from __future__ import annotations

import dataclasses

from hpc_agent.infra.throughput import WorkloadSpec, compute_submission_plan
from hpc_agent.planning.constraints import ClusterConstraints

__all__ = [
    "compact_task_ids",
    "ResubmitBatch",
    "ResubmitPlan",
    "resubmit_plan",
]


def compact_task_ids(ids: list[int]) -> str:
    """Compact a sorted list of task IDs into an array expression.

    Examples
    --------
    ``[1, 2, 3]`` → ``"1-3"``
    ``[3, 7, 12, 13, 14]`` → ``"3,7,12-14"``
    ``[5]`` → ``"5"``

    Raises
    ------
    ValueError
        If ``ids`` is empty.
    """
    if not ids:
        raise ValueError("compact_task_ids requires at least one task id")

    sorted_ids = sorted(ids)
    runs: list[tuple[int, int]] = []
    run_start = sorted_ids[0]
    run_end = run_start
    for tid in sorted_ids[1:]:
        if tid == run_end + 1:
            run_end = tid
        else:
            runs.append((run_start, run_end))
            run_start = tid
            run_end = tid
    runs.append((run_start, run_end))

    parts = [f"{s}" if s == e else f"{s}-{e}" for s, e in runs]
    return ",".join(parts)


@dataclasses.dataclass(frozen=True)
class ResubmitBatch:
    """One batch within a resubmission plan.

    Unlike :class:`~hpc_agent.ops.submit.throughput.JobBatch`, the task IDs in
    a resubmit batch are arbitrary (non-contiguous), so ``task_range`` is
    produced by :func:`compact_task_ids` rather than a simple
    ``"start-end"`` formatter.
    """

    batch_index: int
    task_ids: tuple[int, ...]  # sorted ascending
    wave: int

    @property
    def task_range(self) -> str:
        """Scheduler-compatible 1-based array expression, e.g. ``"4,8,13-15"``.

        ``task_ids`` are 0-based HPC_TASK_IDs (matching the resolve(i)
        contract); the SLURM/SGE templates subtract 1 from
        ``SLURM_ARRAY_TASK_ID``/``SGE_TASK_ID`` to recover the 0-based id.
        Initial submits use ``1-N`` array expressions for exactly this
        reason; resubmits must shift by +1 to stay on the same convention,
        otherwise task ``k`` is retried as task ``k-1``.
        """
        return compact_task_ids([tid + 1 for tid in self.task_ids])

    @property
    def array_size(self) -> int:
        return len(self.task_ids)


@dataclasses.dataclass(frozen=True)
class ResubmitPlan:
    """Complete plan for resubmitting a set of failed task IDs.

    ``overrides`` carries scheduler resource overrides (memory, walltime,
    etc.) that the backend should apply when rendering the job template
    for these batches.  This module does *not* apply overrides itself —
    it only plans the re-submission shape.
    """

    batches: list[ResubmitBatch]
    total_tasks: int  # == number of failed task IDs
    total_batches: int
    max_concurrent: int
    overrides: dict  # scheduler/template overrides; empty dict if none


def resubmit_plan(
    *,
    task_count: int,
    failed_task_ids: list[int],
    overrides: dict | None = None,
    constraints: ClusterConstraints | None = None,
) -> ResubmitPlan:
    """Build a :class:`ResubmitPlan` re-running only the given task IDs.

    Parameters
    ----------
    task_count:
        Total number of tasks in the original run (i.e. ``tasks.total()``
        materialized at submit time, mirrored in the per-run sidecar's
        ``task_count`` field).  Used only to validate that every
        ``failed_task_id`` is in ``range(task_count)``.
    failed_task_ids:
        List of task IDs to retry.  Must be non-empty.  IDs are validated
        as integers in ``[0, task_count)``.
    overrides:
        Optional scheduler overrides (e.g. ``{"mem": "32G",
        "walltime": "12:00:00"}``) attached as metadata on the returned
        plan.  The caller/backend is responsible for applying these to
        the job template.
    constraints:
        Optional cluster constraints governing batching.  Defaults to
        :class:`~hpc_agent.planning.constraints.ClusterConstraints` (i.e.
        ``max_array_size=1000``, ``max_concurrent_jobs=10``).

    Returns
    -------
    ResubmitPlan
        One or more :class:`ResubmitBatch` instances whose ``task_range``
        encodes the failed IDs as a compact ``sbatch``/``qsub`` array
        expression.

    Raises
    ------
    ValueError
        If ``failed_task_ids`` is empty, or if any ID is outside
        ``[0, task_count)``.

    Notes
    -----
    Batching is delegated to
    :func:`~hpc_agent.ops.submit.throughput.compute_submission_plan`: we
    submit a :class:`~hpc_agent.ops.submit.throughput.WorkloadSpec` with
    ``total_tasks = len(failed_task_ids)``, then map each resulting
    ``JobBatch``'s contiguous ``task_start..task_end`` window (1-based
    indexes into the sorted failed list) back to the original task IDs.
    """
    if not failed_task_ids:
        raise ValueError("resubmit_plan requires at least one failed task id")
    if task_count < 0:
        raise ValueError(f"task_count must be non-negative, got {task_count}")

    unknown = [tid for tid in failed_task_ids if not 0 <= int(tid) < task_count]
    if unknown:
        raise ValueError(f"failed_task_ids out of range [0, {task_count}): {unknown}")

    # Dedup: a caller passing duplicate tids would otherwise produce a
    # malformed scheduler array spec like ``"1,1-3"`` (two ``1``s before
    # the contiguous run), which sbatch / qsub reject.
    sorted_ids = sorted({int(tid) for tid in failed_task_ids})
    if constraints is None:
        constraints = ClusterConstraints()

    inner = compute_submission_plan(
        constraints,
        WorkloadSpec(total_tasks=len(sorted_ids)),
    )

    batches: list[ResubmitBatch] = []
    for jb in inner.batches:
        # jb.task_start/task_end are 1-based inclusive indexes into the
        # *sorted failed list* — not into the original task space.
        slice_ids = tuple(sorted_ids[jb.task_start - 1 : jb.task_end])
        batches.append(
            ResubmitBatch(
                batch_index=jb.batch_index,
                task_ids=slice_ids,
                wave=jb.wave,
            )
        )

    return ResubmitPlan(
        batches=batches,
        total_tasks=len(sorted_ids),
        total_batches=inner.total_batches,
        max_concurrent=inner.max_concurrent,
        overrides=dict(overrides) if overrides else {},
    )
