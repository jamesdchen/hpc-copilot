"""``logs`` primitive — fetch per-task stderr from the cluster.

Two task-selection modes:
  *task_ids*: explicit list of task ids
  *all_failed*: re-poll status, fetch logs for failed tasks

Pre-condition: ``SSH_AUTH_SOCK`` must be set; the CLI adapter checks
this before delegating, so the atom assumes a usable SSH agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors, runner
from hpc_agent._internal import session
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent.infra.clusters import load_clusters_config

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="logs",
    verb="query",
    side_effects=[SideEffect("ssh", "<cluster>")],
    error_codes=[
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.JournalCorrupt,
        errors.SpecInvalid,
    ],
    idempotent=True,
    cli="hpc-agent logs --run-id <id> (--task-id <ids> | --all-failed) [--lines <n>]",
    agent_facing=True,
)
def fetch_logs(
    *,
    experiment_dir: Path,
    run_id: str,
    task_ids: list[int] | None = None,
    all_failed: bool = False,
    lines: int = 50,
) -> dict[str, Any]:
    """Fetch stderr logs for selected tasks of a run.

    Exactly one of *task_ids* or *all_failed* must indicate task
    selection — pass *task_ids=[…]* with a non-empty list, or
    *all_failed=True*. The CLI adapter is responsible for parsing the
    user-facing comma-separated ``--task-id`` argument into ``list[int]``.

    Returns ``{"run_id", "scheduler", "logs"[, "note"]}``. The optional
    ``note`` field is set when *all_failed* is True but no failed tasks
    were found in the current status report.
    """
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for run_id {run_id!r}")

    resolved_task_ids: list[int] = []
    note: str | None = None
    if all_failed:
        # Fresh status poll to enumerate failed tasks.
        report = runner._ssh_status_report(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            run_id=run_id,
            job_ids=record.job_ids,
            job_name=record.job_name,
        )
        for tid_str, info in (report.get("tasks") or {}).items():
            if isinstance(info, dict) and info.get("status") == "failed":
                try:
                    resolved_task_ids.append(int(tid_str))
                except (TypeError, ValueError):
                    continue
        if not resolved_task_ids:
            note = "no failed tasks in current status report"
    elif task_ids:
        resolved_task_ids = list(task_ids)
    else:
        raise errors.SpecInvalid("logs requires task_ids=[…] or all_failed=True")

    # Cluster-side scheduler.
    try:
        clusters = load_clusters_config()
    except Exception:  # noqa: BLE001 — config errors fall through to user-error path
        clusters = {}
    scheduler = (clusters.get(record.cluster) or {}).get("scheduler") or "slurm"

    logs: list[dict[str, Any]] = []
    if resolved_task_ids:
        logs = runner.fetch_task_logs(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            job_name=record.job_name,
            job_ids=record.job_ids,
            scheduler=scheduler,
            task_ids=resolved_task_ids,
            lines=int(lines),
        )

    data: dict[str, Any] = {
        "run_id": run_id,
        "scheduler": scheduler,
        "logs": logs,
    }
    if note is not None:
        data["note"] = note
    return data
