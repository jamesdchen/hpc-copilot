"""Throughput optimizer: packs tasks into batched waves for HPC submission.

Given cluster constraints (max array size, max walltime, max concurrent jobs)
and a workload specification, computes an optimal submission plan that
maximizes cluster utilization.
"""

from __future__ import annotations

import dataclasses
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hpc_mapreduce.job.constraints import ClusterConstraints

__all__ = [
    "WorkloadSpec",
    "JobBatch",
    "SubmissionPlan",
    "compute_submission_plan",
]


@dataclasses.dataclass(frozen=True)
class WorkloadSpec:
    """Describes the work to be submitted."""
    total_tasks: int
    est_task_duration_s: int | None = None  # user-provided or from calibration; None = unknown


@dataclasses.dataclass(frozen=True)
class JobBatch:
    """One batch within a submission plan."""
    batch_index: int
    task_start: int          # 1-based inclusive
    task_end: int            # 1-based inclusive
    array_size: int          # task_end - task_start + 1
    est_wall_s: int | None   # estimated wall-clock time, or None if unknown
    wave: int                # which wave this batch belongs to (0-based)

    @property
    def task_range(self) -> str:
        """Scheduler-compatible task range string, e.g. '1-100'."""
        return f"{self.task_start}-{self.task_end}"


@dataclasses.dataclass(frozen=True)
class SubmissionPlan:
    """Complete plan for submitting a workload."""
    batches: list[JobBatch]
    total_tasks: int
    total_batches: int
    max_concurrent: int      # how many batches run in parallel per wave
    est_total_wall_s: int | None  # estimated total wall-clock time
    strategy: str            # human-readable summary


def compute_submission_plan(
    constraints: ClusterConstraints,
    workload: WorkloadSpec,
) -> SubmissionPlan:
    """Compute an optimal submission plan given constraints and workload.

    Algorithm (greedy wave-based packer):
    1. Determine batch count from max_array_size
    2. Check walltime feasibility (if duration known)
    3. Evenly distribute tasks across batches
    4. Group batches into waves of max_concurrent_jobs
    5. Estimate total wall-clock time

    Raises ValueError if a single task exceeds max_walltime.
    """
    total = workload.total_tasks

    # 1. Batch count
    n_batches = math.ceil(total / constraints.max_array_size)

    # 2. Walltime check
    spin_up = constraints.spin_up_seconds()
    walltime_limit = constraints.walltime_seconds()
    effective_time: int | None = None

    if workload.est_task_duration_s is not None:
        effective_time = spin_up + workload.est_task_duration_s
        if effective_time > walltime_limit:
            raise ValueError(
                f"A single task ({effective_time}s incl. spin-up) exceeds "
                f"max walltime ({walltime_limit}s). Consider splitting the "
                f"task into smaller sub-tasks or requesting a longer walltime."
            )

    # 3. Even distribution
    tasks_per_batch = min(math.ceil(total / n_batches), constraints.max_array_size)

    batches: list[JobBatch] = []
    assigned = 0
    for i in range(n_batches):
        task_start = assigned + 1
        remaining = total - assigned
        size = min(tasks_per_batch, remaining)
        task_end = task_start + size - 1
        wave = i // constraints.max_concurrent_jobs

        batches.append(JobBatch(
            batch_index=i,
            task_start=task_start,
            task_end=task_end,
            array_size=size,
            est_wall_s=effective_time,
            wave=wave,
        ))
        assigned += size

    # 4. Wave grouping (already computed per-batch above)
    n_waves = math.ceil(n_batches / constraints.max_concurrent_jobs)

    # 5. Time estimate
    est_total_wall_s: int | None = None
    if effective_time is not None:
        est_total_wall_s = n_waves * effective_time

    # 6. Strategy string
    def _fmt_duration(seconds: int) -> str:
        if seconds >= 3600:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"{h}h{m:02d}m" if m else f"{h}h"
        return f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"

    parts = [
        f"{n_batches} batch{'es' if n_batches != 1 else ''}",
        f"({tasks_per_batch} tasks each)" if n_batches > 1 else f"({total} tasks)",
        f"{constraints.max_concurrent_jobs} concurrent",
        f"{n_waves} wave{'s' if n_waves != 1 else ''}",
    ]
    if est_total_wall_s is not None:
        parts.append(f"~{_fmt_duration(est_total_wall_s)} est.")
    strategy = ", ".join(parts)

    return SubmissionPlan(
        batches=batches,
        total_tasks=total,
        total_batches=n_batches,
        max_concurrent=constraints.max_concurrent_jobs,
        est_total_wall_s=est_total_wall_s,
        strategy=strategy,
    )
