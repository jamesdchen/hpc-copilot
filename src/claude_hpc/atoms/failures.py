"""``failures`` primitive — cluster failed tasks by stderr fingerprint.

Re-polls run status, fetches stderr for each failed task, and
groups them by fingerprint so 40 failures with the same root cause
surface as one cluster instead of 40 separate logs to read.

Pre-condition: ``SSH_AUTH_SOCK`` must be set; the CLI adapter checks
this before delegating, so the atom assumes a usable SSH agent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_hpc import errors, runner
from claude_hpc._internal import session
from claude_hpc._internal.primitive import SideEffect, primitive
from claude_hpc.infra.clusters import load_clusters_config

if TYPE_CHECKING:
    from pathlib import Path


def _resolve_auto_retry(experiment_dir: Path, run_id: str) -> dict[str, dict[str, Any]]:
    """Resolve the auto-retry policy for a run.

    Precedence: per-run sidecar override (``auto_retry`` field, populated
    by /submit when the user supplies a custom policy) > framework
    defaults (:data:`runner.DEFAULT_AUTO_RETRY_POLICY`).

    Always returns a non-empty dict so callers can rely on advice being
    computed for every run.
    """
    try:
        from claude_hpc.state.runs import read_run_sidecar
    except ImportError:
        return dict(runner.DEFAULT_AUTO_RETRY_POLICY)
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return dict(runner.DEFAULT_AUTO_RETRY_POLICY)
    user_policy = sidecar.get("auto_retry")
    if not isinstance(user_policy, dict):
        return dict(runner.DEFAULT_AUTO_RETRY_POLICY)
    valid = {
        cat: pol
        for cat, pol in user_policy.items()
        if isinstance(cat, str) and isinstance(pol, dict)
    }
    return valid or dict(runner.DEFAULT_AUTO_RETRY_POLICY)


@primitive(
    name="failures",
    verb="query",
    side_effects=[SideEffect("ssh", "<cluster>")],
    error_codes=[errors.SshUnreachable],
    idempotent=True,
    cli="hpc-mapreduce failures --run-id <id> [--lines <n>]",
    agent_facing=True,
)
def fetch_failures(
    *,
    experiment_dir: Path,
    run_id: str,
    lines: int = 30,
) -> dict[str, Any]:
    """Cluster failed tasks by stderr fingerprint for triage.

    Re-polls status to enumerate failed task ids, fetches each stderr
    snippet, and groups them by fingerprint. Annotates each cluster
    with retry advice from the resolved auto-retry policy and surfaces
    preempted tasks at the top level for harness branching.
    """
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for run_id {run_id!r}")

    # Fresh poll: enumerate failed tasks.
    report = runner._ssh_status_report(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        run_id=run_id,
        job_ids=record.job_ids,
        job_name=record.job_name,
    )
    failed_ids: list[int] = []
    for tid_str, info in (report.get("tasks") or {}).items():
        if isinstance(info, dict) and info.get("status") == "failed":
            try:
                failed_ids.append(int(tid_str))
            except (TypeError, ValueError):
                continue

    if not failed_ids:
        return {
            "run_id": run_id,
            "failed_count": 0,
            "clusters": [],
            "note": "no failed tasks in current status report",
        }

    # Cluster scheduler.
    try:
        clusters_cfg = load_clusters_config()
    except Exception:  # noqa: BLE001
        clusters_cfg = {}
    scheduler = (clusters_cfg.get(record.cluster) or {}).get("scheduler") or "slurm"

    logs = runner.fetch_task_logs(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        job_name=record.job_name,
        job_ids=record.job_ids,
        scheduler=scheduler,
        task_ids=failed_ids,
        lines=int(lines),
    )
    clusters = runner.cluster_failures_by_fingerprint(logs)

    # Auto-retry policy: resolve per-run sidecar override + framework
    # defaults (runner.DEFAULT_AUTO_RETRY_POLICY). Annotate each cluster
    # with which task ids are still eligible for an automated retry per
    # the per-category max_attempts. Purely advisory — the actual
    # resubmit remains the caller's job (matches existing /resubmit
    # semantics).
    auto_retry = _resolve_auto_retry(experiment_dir, run_id)
    if auto_retry:
        clusters = runner.annotate_clusters_with_retry_advice(
            clusters,
            auto_retry_policy=auto_retry,
            record=record,
        )

    # Surface preempted-task count at the top level so a harness can
    # branch on "campus user got bumped, resubmit cleanly" vs. "real
    # failure, surface to user" without parsing the cluster
    # ``error_class`` strings. Sourced from the failure_signatures
    # catalog entry (exit_code=130 / "[claude-hpc] SIGTERM received"
    # stderr line) — preempted tasks are guaranteed to land in the
    # ``preempted`` cluster.
    preempted_task_ids: list[int] = []
    for cluster in clusters:
        if cluster.get("error_class") == "preempted":
            preempted_task_ids.extend(cluster.get("task_ids") or [])

    data: dict[str, Any] = {
        "run_id": run_id,
        "failed_count": len(failed_ids),
        "clusters": clusters,
        "scheduler": scheduler,
    }
    if preempted_task_ids:
        data["preempted_count"] = len(preempted_task_ids)
        data["preempted_task_ids"] = sorted(preempted_task_ids)
    if auto_retry:
        data["auto_retry_policy"] = auto_retry
    return data
