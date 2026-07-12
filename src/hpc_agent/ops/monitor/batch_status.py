"""``batch-status`` primitive — ONE scheduler query for all in-flight runs.

The connection-storm fix (#2): today every tracked run is polled with its
own ``qstat``/``squeue`` over its own SSH connection, so N runs on the same
login node open N connections per tick — enough to trip fail2ban. Nextflow
and Parsl avoid this by querying the scheduler *once* for all the user's
jobs; this primitive does the same.

It enumerates the journal's in-flight runs, groups them by
``(ssh_target, scheduler)``, and issues a single
``qstat -u $USER`` / ``squeue`` per group over one SSH connection — then
distributes the parsed per-job states back to each run as
``TaskStatus`` values. No on-cluster reporter, no per-run round-trip:
one connection per login node per tick regardless of run count.

Read-only: it queries the scheduler and the journal; it never mutates
the journal (the snapshot is returned for the caller to diff / persist).
Pure-API backends (``requires_ssh=False``) are skipped — they have no
shared login node to batch against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliShape

if TYPE_CHECKING:
    from pathlib import Path


def _resolve_scheduler(cluster: str, clusters_cfg: dict[str, Any]) -> str | None:
    """Resolve a cluster's scheduler name from ``clusters.yaml`` (or ``None``)."""
    entry = clusters_cfg.get(cluster) or {}
    sched = entry.get("scheduler")
    return str(sched) if sched else None


@primitive(
    name="batch-status",
    verb="query",
    side_effects=[
        SideEffect("ssh", "<cluster> (one scheduler-state query per login node)"),
    ],
    error_codes=[errors.SshUnreachable],
    idempotent=True,
    cli=CliShape(
        help=(
            "Poll ALL in-flight runs with one scheduler query per login node "
            "(qstat -u $USER / squeue) instead of one per run — the "
            "connection-storm fix. Returns per-run {job_id: TaskStatus} maps. "
            "Read-only: does not mutate the journal."
        ),
        experiment_dir_arg=True,
        requires_ssh=True,
    ),
    agent_facing=True,
)
def batch_status(*, experiment_dir: Path) -> dict[str, Any]:
    """Query every in-flight run's scheduler state in one query per login node.

    Returns ``{"runs": {run_id: {"job_states": {job_id: TaskStatus.value},
    "missing_job_ids": [...]}}, "queries": <int>, "skipped": [...]}``.

    ``queries`` is the number of SSH scheduler queries issued — the metric
    this primitive exists to minimize: it equals the number of distinct
    ``(ssh_target, scheduler)`` groups, NOT the number of runs.
    ``missing_job_ids`` are a run's job ids absent from the scheduler output
    (left the queue → terminal); the caller decides complete-vs-abandoned by
    cross-checking result files. ``skipped`` names runs that couldn't be
    batched (pure-API backend, or an unresolvable scheduler) — the caller
    falls back to per-run ``poll-run-status`` for those.

    Raises :class:`errors.SshUnreachable` if a login-node query fails — one
    failure does not silently zero the runs that share that login node.
    """
    from hpc_agent.infra.backends import backend_requires_ssh, get_backend_class
    from hpc_agent.infra.cluster_status import ssh_batch_scheduler_states
    from hpc_agent.infra.clusters import load_clusters_config, resolve_ssh_target
    from hpc_agent.state.index import find_in_flight_runs

    records = find_in_flight_runs(experiment_dir)
    try:
        clusters_cfg = load_clusters_config()
    except Exception:  # noqa: BLE001 — degrade to "no config", every run skipped
        clusters_cfg = {}

    # Group runs by the login node + scheduler they share, so each group
    # collapses to a single SSH query. Key on the resolved scheduler too:
    # the same ssh_target could (in principle) front different schedulers.
    groups: dict[tuple[str, str], list[Any]] = {}
    skipped: list[dict[str, str]] = []
    for r in records:
        # Pure-API backends have no shared login node to batch against —
        # leave them to the per-run pure-API status path.
        if r.backend and not backend_requires_ssh(r.backend):
            skipped.append({"run_id": r.run_id, "reason": "pure_api_backend"})
            continue
        scheduler = _resolve_scheduler(r.cluster, clusters_cfg)
        if not scheduler:
            skipped.append({"run_id": r.run_id, "reason": "unresolvable_scheduler"})
            continue
        if not r.job_ids:
            skipped.append({"run_id": r.run_id, "reason": "no_job_ids"})
            continue
        groups.setdefault((resolve_ssh_target(r), scheduler), []).append(r)

    runs_out: dict[str, Any] = {}
    queries = 0
    for (ssh_target, scheduler), group_runs in groups.items():
        backend_cls = get_backend_class(scheduler)
        # One query for the union of every run's job ids on this login node.
        all_job_ids: list[str] = []
        seen: set[str] = set()
        for r in group_runs:
            for jid in r.job_ids:
                sid = str(jid)
                if sid not in seen:
                    seen.add(sid)
                    all_job_ids.append(sid)
        raw_states = ssh_batch_scheduler_states(
            ssh_target=ssh_target,
            backend_cls=backend_cls,
            job_ids=all_job_ids,
        )
        queries += 1
        task_states = backend_cls.batch_status(raw_states)
        # Distribute the shared query's results back to each run.
        for r in group_runs:
            job_states: dict[str, str] = {}
            missing: list[str] = []
            for jid in r.job_ids:
                sid = str(jid)
                if sid in task_states:
                    job_states[sid] = task_states[sid]
                else:
                    missing.append(sid)
            runs_out[r.run_id] = {
                "job_states": job_states,
                "missing_job_ids": missing,
            }

    return {"runs": runs_out, "queries": queries, "skipped": skipped}
