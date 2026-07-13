# @pure: no-io
"""Throughput optimizer: packs tasks into batched waves for HPC submission.

Given cluster constraints (max array size, max walltime, max concurrent jobs)
and a workload specification, computes an optimal submission plan that
maximizes cluster utilization.
"""

from __future__ import annotations

import dataclasses
import math
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.infra.resource_format import coerce

if TYPE_CHECKING:
    from hpc_agent.infra.constraints import ClusterConstraints

__all__ = [
    "WorkloadSpec",
    "JobBatch",
    "SubmissionPlan",
    "compute_submission_plan",
    "build_wave_map",
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
    task_start: int  # 1-based inclusive
    task_end: int  # 1-based inclusive
    array_size: int  # task_end - task_start + 1
    est_wall_s: int | None  # estimated wall-clock time, or None if unknown
    wave: int  # which wave this batch belongs to (0-based)

    @property
    def task_range(self) -> str:
        """Scheduler-compatible task range string, e.g. '1-100'."""
        return f"{self.task_start}-{self.task_end}"


@dataclasses.dataclass(frozen=True)
class SubmissionPlan:
    """Complete plan for submitting a workload.

    ``concurrency_mode`` / ``concurrency_cap`` (#339 item 16) are the
    code-legible DISCLOSURE of *how* this plan bounds concurrency — a field the
    submitter and the ``plan-throughput`` envelope read, never re-derived from
    prose:

    * ``"single-array"`` — one array within the ceiling, no bound needed;
      ``concurrency_cap`` is ``None``.
    * ``"native-cap"`` — one array carrying the scheduler-native in-array cap
      (SLURM ``%N`` / UGE ``-tc N``, = ``concurrency_cap``). The wave split
      existed ONLY to bound concurrency, so a single capped array replaces it:
      the scheduler saturates and back-fills the array with no ``afterany`` wave
      boundary draining to ~zero while stragglers finish.
    * ``"concurrent-arrays"`` — the array-size ceiling forces >1 array but they
      all fit in one concurrent wave (<= ``max_concurrent_jobs``), so there is no
      ``afterany`` chain; ``concurrency_cap`` (when set) is applied WITHIN each
      array.
    * ``"afterany-waves"`` — a genuine multi-wave chain: the array-size ceiling
      forces >1 array AND they span >1 wave (and/or waves carry per-wave
      semantics), so the ``afterany`` wave chain is kept; ``concurrency_cap``
      (when set) is additionally applied WITHIN each wave's arrays.
    """

    batches: list[JobBatch]
    total_tasks: int
    total_batches: int
    max_concurrent: int  # how many batches run in parallel per wave
    est_total_wall_s: int | None  # estimated total wall-clock time
    strategy: str  # human-readable summary
    # #339 item 16 — the disclosed concurrency-bounding decision (see class doc).
    # Defaulted so the hand-built plans (submit-flow's trivial ≤cap plan, the
    # wave-test stubs) that predate item 16 construct unchanged.
    concurrency_mode: str = "single-array"
    concurrency_cap: int | None = None
    concurrency_rationale: str = ""


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
    if total <= 0:
        raise errors.SpecInvalid(
            f"total_tasks must be >= 1 to build a submission plan; got {total}."
        )
    # ``wave = i // max_concurrent_jobs`` and ``ceil(n_batches /
    # max_concurrent_jobs)`` both divide by this; a malformed
    # clusters.yaml with ``max_concurrent_jobs: 0`` would otherwise crash
    # the planner with a low-signal ZeroDivisionError instead of a
    # user-friendly contract error (v3 BUG-4V3-9).
    if constraints.max_concurrent_jobs <= 0:
        raise errors.SpecInvalid(
            "max_concurrent_jobs must be >= 1 to build a submission plan; "
            f"got {constraints.max_concurrent_jobs}."
        )
    if constraints.max_array_size <= 0:
        raise errors.SpecInvalid(
            "max_array_size must be >= 1 to build a submission plan; "
            f"got {constraints.max_array_size}."
        )
    cap = constraints.max_concurrent_tasks
    if cap is not None and cap <= 0:
        raise errors.SpecInvalid(
            f"max_concurrent_tasks must be >= 1 (or unset) to build a submission plan; got {cap}."
        )

    # 1. Batch count
    n_batches = math.ceil(total / constraints.max_array_size)

    # 2. Walltime check
    spin_up = constraints.spin_up_seconds()
    walltime_limit = constraints.walltime_seconds()
    effective_time: int | None = None

    if workload.est_task_duration_s is not None:
        effective_time = spin_up + workload.est_task_duration_s
        # walltime_limit <= 0 means max_walltime is unset or unparseable
        # (parse_walltime_to_sec is permissive and returns 0); skip the
        # check rather than raise a misleading "exceeds 0s" error.
        if walltime_limit > 0 and effective_time > walltime_limit:
            raise errors.SpecInvalid(
                f"A single task ({effective_time}s incl. spin-up) exceeds "
                f"max walltime ({walltime_limit}s). Consider splitting the "
                f"task into smaller sub-tasks or requesting a longer walltime."
            )

    # 3. Even distribution. Round the per-batch share *up* (a remainder
    # task must land in some batch) and clamp to the hard array ceiling —
    # the same "ceil then clamp-to-max" coercion the render layer applies,
    # routed through the shared helper so the policy is auditable in one
    # place. ``coerce(ceil=True)`` yields a whole number at runtime; the
    # ``int(...)`` makes that explicit for the type-checker (the overload's
    # static return is the wider ``int | float``) and is a no-op here.
    tasks_per_batch = int(coerce(total / n_batches, maximum=constraints.max_array_size, ceil=True))

    batches: list[JobBatch] = []
    assigned = 0
    for i in range(n_batches):
        task_start = assigned + 1
        remaining = total - assigned
        size = min(tasks_per_batch, remaining)
        task_end = task_start + size - 1
        wave = i // constraints.max_concurrent_jobs

        batches.append(
            JobBatch(
                batch_index=i,
                task_start=task_start,
                task_end=task_end,
                array_size=size,
                est_wall_s=effective_time,
                wave=wave,
            )
        )
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

    # 7. Concurrency-bounding disclosure (#339 item 16). The single switch is
    # ``n_batches``: exactly one array means the whole sweep fits under the
    # index ceiling, so a native in-array cap (SLURM ``%N`` / UGE ``-tc N``)
    # bounds concurrency with NO ``afterany`` wave boundary (perfect back-fill).
    # More than one array means the ceiling FORCED the split (a genuine
    # multi-array chain / semantic waves) — keep the ``afterany`` chain and
    # optionally cap within each wave.
    if n_batches == 1:
        if cap is not None and cap < total:
            concurrency_mode = "native-cap"
            concurrency_cap: int | None = cap
            concurrency_rationale = (
                f"single array of {total} tasks; concurrency bounded in-array to "
                f"{cap} via the scheduler-native cap (perfect back-fill, no "
                "afterany wave boundary)"
            )
        else:
            concurrency_mode = "single-array"
            concurrency_cap = None
            concurrency_rationale = (
                f"single array of {total} tasks within the {constraints.max_array_size}"
                "-task array ceiling; no concurrency bounding required"
            )
    else:
        # More than one array — the array-size ceiling FORCED the split (a single
        # native-capped array cannot hold > max_array_size tasks). ``n_waves``
        # then says whether the batches also chain: >1 wave is the afterany chain
        # whose boundaries drain to ~zero (the run-#11 76→20 observation); ==1
        # wave means every array fits in one concurrent wave, no afterany
        # boundary. Either way a single native-capped array is impossible, so the
        # native cap (when set) is applied WITHIN each array rather than
        # replacing the split.
        concurrency_cap = cap if (cap is not None and cap > 0) else None
        _within = (
            f"; native cap {concurrency_cap} additionally applied within each array"
            if concurrency_cap
            else ""
        )
        if n_waves > 1:
            concurrency_mode = "afterany-waves"
            concurrency_rationale = (
                f"{n_batches} arrays over {n_waves} afterany-chained waves: the "
                f"{constraints.max_array_size}-task array ceiling forces a "
                "multi-array split, so the afterany wave chain is kept for "
                "cross-array concurrency bounding / per-wave semantics" + _within
            )
        else:
            concurrency_mode = "concurrent-arrays"
            concurrency_rationale = (
                f"{n_batches} arrays in a single concurrent wave (<= "
                f"{constraints.max_concurrent_jobs} max_concurrent_jobs): the "
                f"{constraints.max_array_size}-task array ceiling forces a "
                "multi-array split but no afterany chain is needed" + _within
            )

    return SubmissionPlan(
        batches=batches,
        total_tasks=total,
        total_batches=n_batches,
        max_concurrent=constraints.max_concurrent_jobs,
        est_total_wall_s=est_total_wall_s,
        strategy=strategy,
        concurrency_mode=concurrency_mode,
        concurrency_cap=concurrency_cap,
        concurrency_rationale=concurrency_rationale,
    )


def build_wave_map(plan: SubmissionPlan) -> dict[int, list[int]]:
    """Map wave number to the list of 0-based task IDs belonging to that wave.

    The returned dict is keyed by wave number (``0``, ``1``, …) and each
    value is a sorted list of 0-based task IDs.  This mapping is written
    into the per-run sidecar (``.hpc/runs/<run_id>.json::wave_map``) so
    the on-cluster combiner knows which tasks to aggregate after each
    wave completes.
    """
    wave_map: dict[int, list[int]] = {}
    for batch in plan.batches:
        # batch.task_start/task_end are 1-based inclusive; task IDs are 0-based
        task_ids = list(range(batch.task_start - 1, batch.task_end))
        wave_map.setdefault(batch.wave, []).extend(task_ids)
    return wave_map
