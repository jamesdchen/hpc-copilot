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
from hpc_agent.infra.throughput import WorkloadSpec, build_wave_map, compute_submission_plan

__all__ = ["plan_throughput"]


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
        ),
    ),
    agent_facing=True,
)
def plan_throughput(
    *,
    cluster: str,
    total_tasks: int,
    est_task_duration_s: int | None = None,
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

    Returns
    -------
    ``{strategy, total_tasks, total_batches, max_concurrent, n_waves,
    est_total_wall_s, wave_map, batches}``. ``wave_map`` maps each wave
    number (as a string key, for JSON) to its 0-based task ids;
    ``batches`` lists each array's task range.

    Raises
    ------
    errors.ClusterUnknown
        ``cluster`` is not defined in ``clusters.yaml``.
    errors.SpecInvalid
        ``total_tasks`` < 1, or a single task exceeds the cluster's
        ``max_walltime`` (only checkable when ``est_task_duration_s`` is
        supplied).
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

    wave_map = build_wave_map(plan)
    return {
        "strategy": plan.strategy,
        "total_tasks": plan.total_tasks,
        "total_batches": plan.total_batches,
        "max_concurrent": plan.max_concurrent,
        "n_waves": len(wave_map),
        "est_total_wall_s": plan.est_total_wall_s,
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
