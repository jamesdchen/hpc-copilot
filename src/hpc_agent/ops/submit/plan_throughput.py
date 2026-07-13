"""``plan-throughput`` primitive — pack a task grid into batched submission waves.

Given a cluster's scheduler constraints and a total task count, computes
the wave-batched submission plan: how many scheduler arrays, how they
group into concurrency-bounded waves, and the per-wave task-id map the
cluster-side combiner consumes.

This is the deterministic core of what ``/submit-hpc`` Step 4b used to do
inline — ``compute_submission_plan`` + ``build_wave_map`` are pure
functions over ``(constraints, total_tasks)``, so they belong behind a
primitive the skill (and any headless integrator) invokes, rather than a
block of library calls embedded in skill prose.
"""

from __future__ import annotations

from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.clusters import load_constraints
from hpc_agent.infra.constraints import ClusterConstraints
from hpc_agent.infra.cost import CostEstimate, env_cost_budget, estimate_core_hours
from hpc_agent.infra.throughput import WorkloadSpec, build_wave_map, compute_submission_plan

__all__ = ["evaluate_cost_gate", "plan_throughput"]


def evaluate_cost_gate(
    constraints: ClusterConstraints,
    estimate: CostEstimate,
    *,
    interactive: bool = False,
    budget: float | None = None,
) -> dict[str, Any] | None:
    """Decide the #345 cost/scale gate against a pre-dispatch *estimate*.

    The gate is **off by default**: when ``constraints.max_estimated_core_hours``
    is ``None`` (the default) this returns ``None`` (a no-op — zero behavior
    change). It also returns ``None`` when the estimate is at/under the
    threshold. Only an over-threshold estimate produces a decision:

    * **Interactive** (``interactive=True`` — the slash command passed it):
      return a ``requires_confirmation`` decision block. A pure
      query-primitive cannot block on stdin, so it surfaces the need to
      confirm with the numbers; the interactive caller does the asking.
    * **Unattended** (the default for an agent / headless run): refuse by
      raising :class:`errors.SpecInvalid` with an actionable message —
      UNLESS *budget* (the operator's ``HPC_AGENT_COST_BUDGET`` cap) is set
      and the estimate is under it, in which case the operator has opted into
      the higher cap and the gate returns a ``budget_override`` decision
      block (allowed) instead of refusing.

    **Unknown footprint under a configured ceiling never passes** (run #6):
    when the operator set a threshold but the estimate's footprint is
    unknown (``CostEstimate.footprint_unknown`` — walltime unresolved), the
    defensive ``est_core_hours == 0.0`` must NOT read as "under threshold".
    Interactive → a ``requires_confirmation`` decision block; unattended →
    :class:`errors.SpecInvalid`. The ``HPC_AGENT_COST_BUDGET`` override does
    NOT apply here — an unknown estimate cannot be proven under a budget.

    The returned decision block (when not ``None``) is informational and
    surfaced under the plan's ``cost_gate`` key; it never silently changes
    the wave plan.

    Raises
    ------
    errors.SpecInvalid
        Unattended estimate over threshold and not under an operator budget;
        or an unattended estimate whose footprint is unknown while a
        threshold is configured (never budget-overridable).
    """
    threshold = constraints.max_estimated_core_hours
    if threshold is None:
        return None  # gate disabled — default off, no behavior change.

    est = estimate.est_core_hours
    if estimate.footprint_unknown:
        # The operator configured a core-hours ceiling, but the footprint is
        # unprovable (walltime and/or task count unresolved → the kernel's
        # defensive 0.0). Treating "unknown" as "free" would silently pass
        # an arbitrarily large spend under the ceiling (run #6). No budget
        # override: an unknown estimate cannot be proven under any cap.
        detail = (
            f"({estimate.total_tasks} tasks × {estimate.walltime_s}s walltime) "
            f"while this cluster sets max_estimated_core_hours="
            f"{float(threshold):g}. Set resources.walltime_sec (or pass "
            "--est-task-duration-s) so the footprint can be estimated"
        )
        if interactive:
            return {
                "decision": "requires_confirmation",
                "message": (
                    f"The submission's footprint cannot be estimated {detail}, "
                    "or confirm to proceed with an unbounded footprint."
                ),
                "footprint_unknown": True,
                "est_core_hours": est,
                "est_gpu_hours": estimate.est_gpu_hours,
                "threshold_core_hours": float(threshold),
                "total_tasks": estimate.total_tasks,
                "walltime_s": estimate.walltime_s,
                "cores_per_task": estimate.cores_per_task,
                "gpus_per_task": estimate.gpus_per_task,
            }
        raise errors.SpecInvalid(
            f"the submission's footprint cannot be estimated {detail}. An "
            "unknown footprint is never treated as free under a configured "
            "cost ceiling, and HPC_AGENT_COST_BUDGET cannot override it (an "
            "unknown estimate cannot be proven under a budget). Running "
            "interactively, re-run with confirmation instead."
        )
    if est <= float(threshold):
        return None  # under the operator's ceiling — nothing to gate.

    base = {
        "est_core_hours": est,
        "est_gpu_hours": estimate.est_gpu_hours,
        "threshold_core_hours": float(threshold),
        "total_tasks": estimate.total_tasks,
        "walltime_s": estimate.walltime_s,
        "cores_per_task": estimate.cores_per_task,
        "gpus_per_task": estimate.gpus_per_task,
    }

    if interactive:
        return {
            "decision": "requires_confirmation",
            "message": (
                f"Estimated {est:g} core-hours "
                f"({estimate.total_tasks} tasks × {estimate.walltime_s}s × "
                f"{estimate.cores_per_task} core(s)) exceeds this cluster's "
                f"max_estimated_core_hours threshold ({float(threshold):g}). "
                "Confirm to proceed."
            ),
            **base,
        }

    if budget is not None and est <= budget:
        return {
            "decision": "budget_override",
            "message": (
                f"Estimated {est:g} core-hours exceeds threshold "
                f"({float(threshold):g}) but is within the operator budget "
                f"cap HPC_AGENT_COST_BUDGET={budget:g}; allowed."
            ),
            "budget_core_hours": budget,
            **base,
        }

    over_budget = f" and the operator budget cap ({budget:g})" if budget is not None else ""
    raise errors.SpecInvalid(
        f"estimated submission cost {est:g} core-hours "
        f"({estimate.total_tasks} tasks × {estimate.walltime_s}s × "
        f"{estimate.cores_per_task} core(s)) exceeds this cluster's "
        f"max_estimated_core_hours threshold ({float(threshold):g}){over_budget}. "
        "Reduce the grid (fewer tasks), shorten the per-task walltime, or — if "
        "this spend is intended — raise max_estimated_core_hours in clusters.yaml "
        "or set HPC_AGENT_COST_BUDGET to a core-hours cap that covers it. Running "
        "interactively, re-run with confirmation instead."
    )


@primitive(
    name="plan-throughput",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid, errors.ClusterUnknown],
    idempotent=True,
    cli=CliShape(
        help=(
            "Pack a task grid into batched submission waves. Pure-local: "
            "reads the cluster's constraints from clusters.yaml and returns "
            "the wave plan + wave_map for the per-run sidecar."
        ),
        args=(
            CliArg(
                "--cluster",
                type=str,
                required=True,
                help="Cluster name; its constraints block in clusters.yaml supplies the limits.",
            ),
            CliArg(
                "--total-tasks",
                type=int,
                required=True,
                help="Total task count to pack into waves.",
            ),
            CliArg(
                "--est-task-duration-s",
                type=int,
                default=None,
                help=(
                    "Estimated per-task wall seconds. When given, enables the "
                    "walltime-feasibility check and the total-time estimate."
                ),
            ),
            CliArg(
                "--cores-per-task",
                type=int,
                default=None,
                help=(
                    "Cores each task requests, for the #345 cost estimate "
                    "(defaults to 1 when unknown). Only used when the cluster "
                    "sets max_estimated_core_hours."
                ),
            ),
            CliArg(
                "--gpus-per-task",
                type=int,
                default=None,
                help="GPUs each task requests, for the #345 cost estimate (0 for CPU-only).",
            ),
            CliArg(
                "--interactive",
                action="store_true",
                help=(
                    "An interactive caller (slash command). When the #345 cost "
                    "gate trips, surface a confirmation request instead of "
                    "refusing with spec_invalid (the unattended/agent default)."
                ),
            ),
        ),
    ),
    agent_facing=True,
)
def plan_throughput(
    *,
    cluster: str,
    total_tasks: int,
    est_task_duration_s: int | None = None,
    cores_per_task: int | None = None,
    gpus_per_task: int | None = None,
    interactive: bool = False,
) -> dict[str, Any]:
    """Compute the wave-batched submission plan for *total_tasks* on *cluster*.

    Loads the cluster's scheduler constraints from ``clusters.yaml``
    (``max_array_size`` / ``max_walltime`` / ``max_concurrent_jobs`` /
    ``est_spin_up``), packs ``total_tasks`` into batched waves, and
    returns the plan plus the ``wave_map`` the per-run sidecar carries
    for the cluster-side combiner.

    Parameters
    ----------
    cluster:
        Cluster name; its ``constraints:`` block in ``clusters.yaml``
        supplies the scheduler limits. A cluster with no such block
        falls back to :class:`~hpc_agent.infra.constraints.ClusterConstraints`
        defaults.
    total_tasks:
        Total task count to pack (the grid cardinality).
    est_task_duration_s:
        Optional estimated per-task wall seconds. When supplied it
        enables the walltime-feasibility check and the total-time
        estimate; when omitted the plan is structural only.
    cores_per_task:
        Optional cores-per-task for the #345 cost estimate (defaults to 1
        when unknown). Only consulted when the cluster sets
        ``max_estimated_core_hours``.
    gpus_per_task:
        Optional GPUs-per-task for the #345 cost estimate (0 for CPU-only).
    interactive:
        Whether an interactive caller invoked this. When the cost gate
        trips, an interactive caller is handed a confirmation request; an
        unattended caller (the default) is refused with ``spec_invalid``
        unless under ``HPC_AGENT_COST_BUDGET``.

    Returns
    -------
    ``{strategy, total_tasks, total_batches, max_concurrent, n_waves,
    est_total_wall_s, wave_map, batches, cost_gate?}``. ``wave_map`` maps each
    wave number (as a string key, for JSON) to its 0-based task ids;
    ``batches`` lists each array's task range. ``cost_gate`` is present only
    when the #345 gate is active *and* the estimate crosses the cluster's
    ``max_estimated_core_hours`` threshold (interactive confirmation request,
    or an operator-budget override); it is **absent** in the default
    off/under-threshold case, so behavior is unchanged unless an operator
    opts in.

    Raises
    ------
    errors.ClusterUnknown
        ``cluster`` is not defined in ``clusters.yaml``.
    errors.SpecInvalid
        ``total_tasks`` < 1; a single task exceeds the cluster's
        ``max_walltime`` (only checkable when ``est_task_duration_s`` is
        supplied); or — when the cluster sets ``max_estimated_core_hours``
        and this is an unattended over-threshold submission not under
        ``HPC_AGENT_COST_BUDGET`` — the #345 cost gate. An unattended
        submission whose footprint is UNKNOWN (walltime unresolved) is also
        refused when the threshold is set, and is never budget-overridable.
    """
    from hpc_agent import load_clusters_config

    clusters = load_clusters_config()
    cluster_cfg = clusters.get(cluster)
    if not isinstance(cluster_cfg, dict):
        raise errors.ClusterUnknown(
            f"cluster {cluster!r} is not defined in clusters.yaml; "
            f"known clusters: {sorted(clusters)}"
        )

    constraints = load_constraints(cluster_cfg)
    workload = WorkloadSpec(total_tasks=int(total_tasks), est_task_duration_s=est_task_duration_s)
    try:
        plan = compute_submission_plan(constraints, workload)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc

    # #345 cost/scale gate. Default off: only fires when the operator set
    # ``max_estimated_core_hours`` on this cluster (evaluate_cost_gate is a
    # no-op otherwise). The pre-dispatch walltime is the per-task duration
    # when supplied, else the cluster's hard ``max_walltime`` ceiling — the
    # conservative worst-case footprint a task could consume.
    walltime_s = (
        int(est_task_duration_s)
        if est_task_duration_s is not None
        else constraints.walltime_seconds()
    )
    estimate = estimate_core_hours(
        total_tasks=int(total_tasks),
        walltime_s=walltime_s,
        cores_per_task=cores_per_task,
        gpus_per_task=gpus_per_task,
    )
    cost_gate = evaluate_cost_gate(
        constraints,
        estimate,
        interactive=interactive,
        budget=env_cost_budget(),
    )

    wave_map = build_wave_map(plan)
    result: dict[str, Any] = {
        "strategy": plan.strategy,
        "total_tasks": plan.total_tasks,
        "total_batches": plan.total_batches,
        "max_concurrent": plan.max_concurrent,
        "n_waves": len(wave_map),
        "est_total_wall_s": plan.est_total_wall_s,
        # #339 item 16: the code-legible concurrency-bounding decision. A
        # ``native-cap`` sweep fits in one array and is bounded by the
        # scheduler-native in-array cap (SLURM ``%N`` / UGE ``-tc N``) with no
        # afterany wave boundary; ``afterany-waves`` keeps the wave chain (the
        # array ceiling forced a multi-array split / waves carry semantics).
        # ``concurrency_cap`` is the emitted N (``None`` when no native cap).
        "concurrency_mode": plan.concurrency_mode,
        "concurrency_cap": plan.concurrency_cap,
        "concurrency_rationale": plan.concurrency_rationale,
        # JSON object keys must be strings; the combiner reads them back
        # from the per-run sidecar where they are already stringified.
        "wave_map": {str(wave): ids for wave, ids in sorted(wave_map.items())},
        "batches": [
            {
                "batch_index": b.batch_index,
                "task_range": b.task_range,
                "array_size": b.array_size,
                "wave": b.wave,
                "est_wall_s": b.est_wall_s,
            }
            for b in plan.batches
        ],
    }
    # Surfaced only when the gate produced a decision (interactive
    # confirmation request or operator-budget override). Absent in the
    # default off / under-threshold case so the envelope is byte-identical
    # to the pre-#345 shape when no operator opted in.
    if cost_gate is not None:
        result["cost_gate"] = cost_gate
    return result
