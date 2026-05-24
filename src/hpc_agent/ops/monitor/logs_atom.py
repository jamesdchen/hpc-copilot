"""``logs`` primitive — fetch per-task stderr from the cluster.

Two task-selection modes:
  *task_ids*: explicit list of task ids
  *all_failed*: re-poll status, fetch logs for failed tasks

Pre-condition: ``SSH_AUTH_SOCK`` must be set; the CLI adapter checks
this before delegating, so the atom assumes a usable SSH agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._internal import session
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.clusters import load_clusters_config
from hpc_agent.ops.monitor.logs import fetch_task_logs
from hpc_agent.ops.monitor.status import _ssh_status_report

if TYPE_CHECKING:
    import argparse
    from pathlib import Path


def _logs_arg_pre(ns: argparse.Namespace) -> dict[str, Any]:
    """Parse ``--task-id "1,2,3"`` → ``list[int]`` and enforce the mutex.

    ``--all-failed`` and ``--task-id`` select different task sets; the
    legacy adapter silently preferred ``--all-failed`` when both were
    supplied. The mutex is enforced explicitly here so a caller who
    sets both sees a clear ``spec_invalid`` instead of a silent demotion.
    """
    raw = getattr(ns, "task_ids", None)
    all_failed = bool(getattr(ns, "all_failed", False))
    if all_failed and raw:
        raise errors.SpecInvalid("--all-failed and --task-id are mutually exclusive")
    if raw:
        try:
            parsed = [int(x.strip()) for x in str(raw).split(",") if x.strip()]
        except ValueError as exc:
            raise errors.SpecInvalid(f"--task-id must be comma-separated integers: {exc}") from exc
        if not parsed:
            raise errors.SpecInvalid("--task-id is empty")
        return {"task_ids": parsed}
    return {"task_ids": None}


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
    cli=CliShape(
        help=("Fetch per-task stderr logs from the cluster (requires --task-id or --all-failed)."),
        requires_ssh=True,
        experiment_dir_arg=True,
        args=(
            CliArg("--run-id", type=str, required=True),
            CliArg(
                "--task-id",
                type=str,
                default=None,
                dest="task_ids",
                help="Comma-separated task ids to fetch (e.g. '7,12,42').",
            ),
            CliArg(
                "--all-failed",
                action="store_true",
                help="Re-poll status and fetch logs for every task with status=failed.",
            ),
            CliArg(
                "--lines",
                type=int,
                default=50,
                help="Number of trailing lines to return per log (default 50).",
            ),
        ),
        arg_pre=_logs_arg_pre,
    ),
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
        report = _ssh_status_report(
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
    scheduler = (clusters.get(record.cluster) or {}).get("scheduler")
    if not scheduler:
        raise errors.SpecInvalid(
            f"cannot resolve scheduler for cluster {record.cluster!r}: "
            f"absent from clusters.yaml or missing a 'scheduler' key — refusing "
            f"to guess 'slurm' and risk misrouting the SGE log fetch"
        )

    logs: list[dict[str, Any]] = []
    if resolved_task_ids:
        logs = fetch_task_logs(
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
