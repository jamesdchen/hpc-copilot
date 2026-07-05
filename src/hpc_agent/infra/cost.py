# @pure: no-io
"""Compute-cost arithmetic: the one place footprint → core-hours is defined.

#345 — cost-aware submission. Two consumers share the *same* normalization
so the pre-dispatch estimate and the post-run actual speak one unit:

* **Pre-dispatch estimate** — the cost/scale gate in
  :mod:`hpc_agent.ops.submit.plan_throughput` calls :func:`estimate_core_hours`
  to turn ``tasks × walltime × cores[/gpus]`` into estimated core-hours
  *before* anything is submitted.
* **Post-run actual** — :func:`hpc_agent.execution.mapreduce.reduce.metrics.reduce_resource_usage`
  calls :func:`core_hours_from_cpu_seconds` to normalize the per-task
  ``cpu_s`` the scheduler accounting parsers (``query.query_sacct`` /
  ``query_sge`` / ``query_pbs``) already emit into the same core-hours unit.

The normalization is deliberately the same identity the campaign budget
accounting already uses (``compute_spend.consumed_compute_for_campaign`` /
``runtime_prior.cores_used_from_sample``):

    core_hours = elapsed_s × effective_cores / 3600
               = cpu_s / 3600           (since cpu_s = cores × elapsed_s)

so "core-hours" and the existing ``cpu_hours`` rollup are the *same number*
viewed through the issue's cost vocabulary — surfaced under both names rather
than recomputed a second, drift-prone way.

**Off by default.** Nothing here changes behavior on its own: the estimator
is a pure function, and the budget env (:func:`env_cost_budget`) and the
per-cluster threshold (``ClusterConstraints.max_estimated_core_hours``) are
both *unset* by default, leaving the gate a no-op (see ``plan_throughput``).

Stdlib only — importable on every runtime surface; correctness is testable
against captured numbers without any scheduler installed.
"""

from __future__ import annotations

import dataclasses
import os

__all__ = [
    "CostEstimate",
    "core_hours_from_cpu_seconds",
    "env_cost_budget",
    "estimate_core_hours",
    "gpu_hours_from_gpu_seconds",
]

# Operator env override: a numeric core-hours budget cap that lets an
# unattended run exceed a cluster's ``max_estimated_core_hours`` threshold.
# Unset / blank / unparseable → None (no override), keeping the gate a no-op
# unless an operator both sets a per-cluster threshold AND opts into a cap.
_COST_BUDGET_ENV = "HPC_AGENT_COST_BUDGET"

_SECONDS_PER_HOUR = 3600.0


@dataclasses.dataclass(frozen=True)
class CostEstimate:
    """A pre-dispatch submission-footprint estimate, in normalized hours.

    Fields
    ------
    total_tasks:
        Grid cardinality the estimate is over.
    walltime_s:
        Per-task wall-clock budget (seconds) the estimate assumes — the
        ceiling, not a measured runtime, since this is a *pre*-dispatch
        worst-case footprint.
    cores_per_task:
        Cores each task requests (defaults to 1 when unknown, the
        conservative floor — a real grid asks for at least one core).
    gpus_per_task:
        GPUs each task requests (0 for CPU-only work).
    est_core_hours:
        ``tasks × walltime_s × cores_per_task / 3600``.
    est_gpu_hours:
        ``tasks × walltime_s × gpus_per_task / 3600`` (0 when CPU-only).
    """

    total_tasks: int
    walltime_s: int
    cores_per_task: int
    gpus_per_task: int
    est_core_hours: float
    est_gpu_hours: float

    @property
    def footprint_unknown(self) -> bool:
        """True when the estimate carries no real footprint.

        A non-positive ``walltime_s`` or ``total_tasks`` means
        :func:`estimate_core_hours` returned its *defensive* zero — nothing
        was measured, so ``est_core_hours == 0.0`` reads as "unknown", NOT
        "free" (proving run #6: a cold-start submit with an unresolved
        walltime rendered "est. 0 core-hours" to the human). Derived, not
        stored: arithmetic consumers keep the zero-return contract, while
        render/gate consumers branch on this to say "unknown" and to refuse
        passing an unprovable footprint under a configured cost ceiling.
        """
        return self.walltime_s <= 0 or self.total_tasks <= 0


def core_hours_from_cpu_seconds(cpu_s: float) -> float:
    """Normalize summed per-task ``cpu_s`` into core-hours.

    ``cpu_s`` is already ``cores × elapsed_s`` per task (see
    ``query.query_sacct`` / ``query_sge`` / ``query_pbs``), so the cost is a
    plain ``/3600``. Kept here, not inlined, so the post-run actual and the
    campaign budget accounting cannot drift onto different definitions of a
    core-hour. Negative / nonsense input clamps to 0.0.
    """
    if cpu_s <= 0:
        return 0.0
    return round(cpu_s / _SECONDS_PER_HOUR, 4)


def gpu_hours_from_gpu_seconds(gpu_s: float) -> float:
    """Normalize summed per-task ``gpu_s`` into GPU-hours (cost-vocabulary twin)."""
    if gpu_s <= 0:
        return 0.0
    return round(gpu_s / _SECONDS_PER_HOUR, 4)


def estimate_core_hours(
    total_tasks: int,
    walltime_s: int,
    cores_per_task: int | None = None,
    gpus_per_task: int | None = None,
) -> CostEstimate:
    """Estimate a submission's compute footprint as core-hours (pre-dispatch).

    ``tasks × walltime × cores[/gpus] → core-hours``. This is the *only*
    footprint→cost mapping; the cost/scale gate consumes it, and the
    post-run actual reuses :func:`core_hours_from_cpu_seconds` so both halves
    speak the same unit.

    Defensive on inputs: a non-positive task count or walltime yields a
    zero-cost estimate (nothing to charge) rather than raising — the caller's
    own validation (e.g. ``total_tasks >= 1``) owns rejecting those; this
    helper is a pure arithmetic kernel. ``cores_per_task``/``gpus_per_task``
    default to the conservative floor (1 core, 0 gpus) when unknown.

    Parameters
    ----------
    total_tasks:
        Grid cardinality.
    walltime_s:
        Per-task wall-clock ceiling in seconds (worst-case footprint).
    cores_per_task:
        Cores each task requests; ``None``/``<1`` → 1 (the floor).
    gpus_per_task:
        GPUs each task requests; ``None``/``<0`` → 0.

    Returns
    -------
    A :class:`CostEstimate` carrying the inputs + ``est_core_hours`` /
    ``est_gpu_hours``.
    """
    tasks = max(0, int(total_tasks))
    wall = max(0, int(walltime_s))
    cores = max(1, int(cores_per_task)) if cores_per_task else 1
    gpus = max(0, int(gpus_per_task)) if gpus_per_task else 0

    core_seconds = float(tasks) * float(wall) * float(cores)
    gpu_seconds = float(tasks) * float(wall) * float(gpus)
    return CostEstimate(
        total_tasks=tasks,
        walltime_s=wall,
        cores_per_task=cores,
        gpus_per_task=gpus,
        est_core_hours=round(core_seconds / _SECONDS_PER_HOUR, 4),
        est_gpu_hours=round(gpu_seconds / _SECONDS_PER_HOUR, 4),
    )


def env_cost_budget() -> float | None:
    """Operator core-hours budget cap from ``HPC_AGENT_COST_BUDGET``.

    Unset / blank → ``None`` (no override; the gate keeps its default
    refuse-over-threshold posture). A parseable non-negative number is the
    cap under which an unattended run may exceed a cluster's
    ``max_estimated_core_hours`` threshold. A negative or unparseable value
    is treated as unset (``None``) — an operator who fat-fingers the cap
    gets the safe default, not an accidental "infinite budget".
    """
    raw = os.environ.get(_COST_BUDGET_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value
