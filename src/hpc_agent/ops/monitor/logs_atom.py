"""``logs`` primitive — fetch per-task stderr from the cluster.

Two task-selection modes:
  *task_ids*: explicit list of task ids
  *all_failed*: re-poll status, fetch logs for failed tasks

Pre-condition: ``SSH_AUTH_SOCK`` must be set; the CLI adapter checks
this before delegating, so the atom assumes a usable SSH agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.backends import backend_requires_ssh
from hpc_agent.infra.clusters import load_clusters_config
from hpc_agent.ops.monitor.logs import fetch_task_logs
from hpc_agent.ops.monitor.status import _ssh_status_report
from hpc_agent.state.journal import load_run
from hpc_agent.state.run_record import runs_dir

if TYPE_CHECKING:
    import argparse


def _pure_api_log_entries(written: str) -> list[dict[str, Any]]:
    """Normalize a pure-API ``fetch_logs`` return into per-file log entries.

    A pure-API backend hands back a path to the run's fetched logs — often a
    single archive (GitHub returns one job-logs ``.zip``). Unpack an archive so
    the caller gets browsable, greppable files instead of an opaque blob, and
    return one ``{"path": ...}`` entry per file (a directory is listed as-is; a
    plain file is returned as the single entry). Deliberately does NOT synthesize
    a ``task_id`` per entry the way the SSH path does — a pure-API backend's logs
    are run-level, not per-task-addressable; the ``note`` on the envelope says so.
    """
    import zipfile

    p = Path(written)
    if p.is_file() and p.suffix == ".zip":
        out_dir = p.with_suffix("")
        try:
            with zipfile.ZipFile(p) as zf:
                zf.extractall(out_dir)
        except (zipfile.BadZipFile, OSError):
            return [{"path": str(p)}]  # not a readable archive — hand back the path
        files = sorted(str(f) for f in out_dir.rglob("*") if f.is_file())
        return [{"path": f} for f in files] or [{"path": str(out_dir)}]
    if p.is_dir():
        files = sorted(str(f) for f in p.rglob("*") if f.is_file())
        return [{"path": f} for f in files] or [{"path": str(p)}]
    return [{"path": str(p)}]


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
    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for run_id {run_id!r}")

    if not backend_requires_ssh(record.backend):
        # Pure-API path (#337 Increment 4): no login node to ``ssh tail`` a
        # per-task stderr path. The backend's ``fetch_logs`` instance hook pulls
        # the run's logs over its API into a local dir; task-id selection is
        # advisory (the API returns the run's logs as a unit). Zero SSH.
        from hpc_agent.infra.backends.remote_factory import backend_for_record

        dest = runs_dir(experiment_dir) / f"{run_id}-logs"
        written = backend_for_record(record).fetch_logs(run_id, str(dest))
        return {
            "run_id": run_id,
            "scheduler": record.backend,
            "logs": _pure_api_log_entries(written),
            "note": (
                "pure-API backend: run-level logs fetched over the API (no per-task "
                "stderr addressing). Files named 'task-<i>' map to task ids."
            ),
        }

    resolved_task_ids: list[int] = []
    note: str | None = None
    if all_failed:
        # Fresh status poll to enumerate failed tasks. Seed the run's cluster
        # env activation exactly as ``record_status`` does — the reporter runs
        # on the login node via ssh_run and would otherwise hit the bare
        # login-node python that lacks hpc_agent (rc=127 on conda clusters, the
        # run-#7/#8 class). The journal record always knows the cluster; backfill
        # it into the sidecar when the sidecar carries none so the deriver's
        # cluster-backfill arm fires (#281). Sibling of the record_status seed.
        from hpc_agent.infra.clusters import remote_activation_for_sidecar
        from hpc_agent.state.runs import read_run_sidecar

        try:
            _sidecar = read_run_sidecar(experiment_dir, run_id)
        except (OSError, json.JSONDecodeError, errors.HpcError):
            _sidecar = {}
        if record.cluster and not _sidecar.get("cluster"):
            _sidecar = {**_sidecar, "cluster": record.cluster}
        report = _ssh_status_report(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            run_id=run_id,
            job_ids=record.job_ids,
            job_name=record.job_name,
            remote_activation=remote_activation_for_sidecar(_sidecar),
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
    except OSError:  # unreadable config → empty; a missing scheduler then fails loud below
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
        from hpc_agent.state.runs import read_job_task_spans

        logs = fetch_task_logs(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            job_name=record.job_name,
            job_ids=record.job_ids,
            scheduler=scheduler,
            task_ids=resolved_task_ids,
            lines=int(lines),
            # Waved runs: the sidecar's per-job global task windows route each
            # probe to the covering job with the job-LOCAL log index. None
            # (old sidecar / single array / resubmit job) keeps the global
            # probe — read_job_task_spans never raises.
            job_task_spans=read_job_task_spans(experiment_dir, run_id),
        )

    data: dict[str, Any] = {
        "run_id": run_id,
        "scheduler": scheduler,
        "logs": logs,
    }
    if note is not None:
        data["note"] = note
    return data
