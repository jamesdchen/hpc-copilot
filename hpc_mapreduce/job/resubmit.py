"""Resubmission helpers: re-run only the failed task IDs from a prior job.

When a grid job finishes with some failed tasks, ``/monitor`` (and the LLM)
needs to resubmit exactly those task IDs — possibly under adjusted resources
(more memory, a longer walltime) — without duplicating the on-cluster
batching logic that lives in :mod:`hpc_mapreduce.job.throughput`.

This module provides:

* :func:`compact_task_ids` — pack a sorted list of 1-based task IDs into an
  ``sbatch``/``qsub`` array expression (``"3,7,12-14"`` etc.).
* :class:`ResubmitPlan` / :class:`ResubmitBatch` — the resubmission
  analogue of :class:`~hpc_mapreduce.job.throughput.SubmissionPlan`, whose
  ``task_range`` strings can be *non-contiguous* since the failed IDs are
  arbitrary.
* :func:`resubmit_plan` — build a :class:`ResubmitPlan` from a manifest and
  a list of failed task IDs, reusing
  :func:`~hpc_mapreduce.job.throughput.compute_submission_plan` to split the
  failures into batches that honour the cluster's ``max_array_size`` and
  ``max_concurrent_jobs`` limits.

Only stdlib imports — keeps the library dep-free.
"""

from __future__ import annotations

import dataclasses

from hpc_mapreduce.job.constraints import ClusterConstraints
from hpc_mapreduce.job.throughput import WorkloadSpec, compute_submission_plan

__all__ = [
    "compact_task_ids",
    "ResubmitBatch",
    "ResubmitPlan",
    "resubmit_plan",
]


def compact_task_ids(ids: list[int]) -> str:
    """Compact a sorted list of 1-based task IDs into an array expression.

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

    Unlike :class:`~hpc_mapreduce.job.throughput.JobBatch`, the task IDs in
    a resubmit batch are arbitrary (non-contiguous), so ``task_range`` is
    produced by :func:`compact_task_ids` rather than a simple
    ``"start-end"`` formatter.
    """

    batch_index: int
    task_ids: tuple[int, ...]  # 1-based, sorted
    wave: int

    @property
    def task_range(self) -> str:
        """Scheduler-compatible array expression, e.g. ``"3,7,12-14"``."""
        return compact_task_ids(list(self.task_ids))

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
    manifest: dict,
    failed_task_ids: list[int],
    overrides: dict | None = None,
    constraints: ClusterConstraints | None = None,
) -> ResubmitPlan:
    """Build a :class:`ResubmitPlan` re-running only the given task IDs.

    Parameters
    ----------
    manifest:
        The original task manifest (as produced by
        :func:`~hpc_mapreduce.job.grid.build_task_manifest`).  Used only to
        validate that every ``failed_task_id`` is known — commands and
        ``result_dir`` entries are reused directly by the backend at
        submit time.
    failed_task_ids:
        List of 1-based (or 0-based — see note) task IDs to retry.  The
        manifest uses string keys; IDs are converted to strings when
        looking them up.  Must be non-empty.
    overrides:
        Optional scheduler overrides (e.g. ``{"mem": "32G",
        "walltime": "12:00:00"}``) to be attached as metadata on the
        returned plan.  The caller/backend is responsible for applying
        these to the job template.
    constraints:
        Optional cluster constraints governing batching.  Defaults to
        :class:`~hpc_mapreduce.job.constraints.ClusterConstraints` (i.e.
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
        If ``failed_task_ids`` is empty, or if any ID is absent from
        ``manifest["tasks"]``.

    Notes
    -----
    Batching is delegated to
    :func:`~hpc_mapreduce.job.throughput.compute_submission_plan`: we
    submit a :class:`~hpc_mapreduce.job.throughput.WorkloadSpec` with
    ``total_tasks = len(failed_task_ids)``, then map each resulting
    ``JobBatch``'s contiguous ``task_start..task_end`` window (which
    indexes into the sorted failed list, 1-based) back to the original
    task IDs.  This avoids duplicating wave/max_array_size logic.
    """
    if not failed_task_ids:
        raise ValueError("resubmit_plan requires at least one failed task id")

    tasks = manifest.get("tasks", {})

    # Validate every failed id is known in the manifest.
    unknown: list[int] = []
    for tid in failed_task_ids:
        if str(tid) not in tasks:
            unknown.append(tid)
    if unknown:
        raise ValueError(f"failed_task_ids not present in manifest: {unknown}")

    sorted_ids = sorted(failed_task_ids)
    if constraints is None:
        constraints = ClusterConstraints()

    # Delegate batching to the throughput planner.  It treats the failed
    # count as a fresh workload of N contiguous tasks; we then translate
    # each batch's 1-based index window back to the actual IDs.
    inner = compute_submission_plan(
        constraints,
        WorkloadSpec(total_tasks=len(sorted_ids)),
    )

    batches: list[ResubmitBatch] = []
    for jb in inner.batches:
        # jb.task_start/task_end are 1-based inclusive indexes into the
        # *sorted failed list* — not into the original manifest.
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
