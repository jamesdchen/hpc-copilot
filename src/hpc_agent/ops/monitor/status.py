"""Status-reporting runner primitives."""

from __future__ import annotations

import argparse
import contextlib
import json
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.journal import update_run_status
from hpc_agent.state.run_record import RunRecord, _atomic_write_json, runs_dir

if TYPE_CHECKING:
    from pathlib import Path


def _status_handler(ns: argparse.Namespace) -> int:
    """Tier 2 escape hatch — composite envelope folds sidecar + journal data.

    The hand-written body lives in :mod:`hpc_agent.cli.lifecycle` to
    keep the runner module focused on the primitive itself; the
    handler closure on this side is only the deferred-import shim
    the dispatcher invokes.
    """
    from hpc_agent.cli.lifecycle import cmd_status

    return cmd_status(ns)


# Canonical implementation lives in ``infra/cluster_status.py`` so the
# aggregate / recover subjects can reach it without crossing into the
# monitor subject. Re-exported here under both the public name and the
# underscore-prefixed back-compat alias so package-internal callers
# (``reconcile``, the local ``record_status`` primitive below) keep
# working unchanged.
from hpc_agent.infra.cluster_status import ssh_status_report  # noqa: E402

_ssh_status_report = ssh_status_report


@primitive(
    name="poll-run-status",
    verb="query",
    side_effects=[
        SideEffect("ssh", "<cluster>"),
        SideEffect(
            "writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (refreshes last_status)"
        ),
    ],
    error_codes=[errors.JournalCorrupt, errors.SshUnreachable, errors.RemoteCommandFailed],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help="Poll cluster status for a run_id; one-shot, returns snapshot.",
        verb="status",
        requires_ssh=True,
        experiment_dir_arg=True,
        args=(
            CliArg("--run-id", type=str, required=True),
            CliArg(
                "--min-rows",
                type=int,
                default=0,
                help=(
                    "Require each task's CSV result to have at least N data rows "
                    "beyond the header. A completed task with fewer rows is demoted "
                    "complete -> failed. Default 0 accepts header-only CSVs."
                ),
            ),
        ),
        handler=_status_handler,
    ),
    agent_facing=True,
)
def record_status(
    experiment_dir: Path,
    run_id: str,
    *,
    ssh_target: str,
    remote_path: str,
    job_ids: list[str],
    job_name: str,
    file_glob: str = "*",
    min_rows: int = 0,
) -> RunRecord:
    """Run the status reporter and write ``last_status`` to the journal.

    The cluster-side reporter reads ``.hpc/runs/<run_id>.json`` for run
    metadata and ``.hpc/tasks.py`` for per-task kwargs.

    Also writes the snapshot to ``<run_id>.last_status.json`` next to the
    journal record so any consumer (agent, human, ``jq`` pipeline, file
    watcher) can read the latest cached state without re-issuing an SSH
    call. The file's mtime tells the caller how stale the snapshot is.

    ``min_rows`` is forwarded to the cluster-side reporter (see
    :func:`_ssh_status_report`): a completed task whose CSV result has
    fewer than ``min_rows`` data rows is demoted ``complete`` → ``failed``,
    so a caller can gate on "every task wrote real data" rather than just
    "every task wrote a file".
    """
    # Activate the run's cluster env (conda/modules) for the control-plane
    # reporter — it runs directly on the login node via ssh_run and would
    # otherwise hit the bare login-node python that lacks the framework.
    from hpc_agent.infra.clusters import remote_activation_for_sidecar
    from hpc_agent.state.runs import read_run_sidecar

    try:
        _sidecar = read_run_sidecar(experiment_dir, run_id)
    except (
        OSError,
        json.JSONDecodeError,
        errors.HpcError,
    ):  # missing/bad sidecar → bare python; a bug propagates
        _sidecar = {}

    report = _ssh_status_report(
        ssh_target=ssh_target,
        remote_path=remote_path,
        run_id=run_id,
        job_ids=job_ids,
        job_name=job_name,
        file_glob=file_glob,
        min_rows=min_rows,
        remote_activation=remote_activation_for_sidecar(_sidecar),
    )
    summary = dict(report.get("summary", {}))
    summary["checked_at"] = utcnow_iso()
    # Carry per-wave breakdown into the persisted last_status when the
    # cluster-side reporter emitted one (sidecar carried a wave_map).
    if isinstance(report.get("waves"), dict) and report["waves"]:
        summary["waves"] = report["waves"]
    # Carry the fresh scheduler-side preemption signal (exit 130/143 / state
    # PREEMPTED) into last_status so the monitor's auto-resume gate (#299)
    # reads it without a second round-trip. These are *report-space* ids
    # (1-based scheduler array indices, matching report["tasks"] keys); the
    # auto-resume composite converts them to 0-based HPC_TASK_ID for resubmit.
    # Present only when the reporter found preempted tasks; absent → the
    # composite falls back to a log-based fetch (cross-scheduler, e.g. SGE
    # without exit codes).
    preempted_ids = report.get("preempted_task_ids")
    if isinstance(preempted_ids, list) and preempted_ids:
        summary["preempted_task_ids"] = preempted_ids
    record = update_run_status(experiment_dir, run_id, last_status=summary)
    # Cache the snapshot for cheap external reads. Best-effort: a write
    # failure here must not roll back the journal update.
    cache_path = runs_dir(experiment_dir) / f"{run_id}.last_status.json"
    # Atomic write so a concurrent reader never sees a half-written
    # file.  ``Path.write_text`` truncates in place; readers that
    # race with the writer would otherwise observe a JSONDecodeError.
    #
    # ``fsync=False`` because the cache is a strict denormalization of
    # the journal record's ``last_status`` field — ``update_run_status``
    # above already fsync'd that. On a kernel-panic/power-loss between
    # the two writes the cache may revert, but the next monitor tick
    # rewrites it from the still-durable journal. Halves per-tick fsync
    # cost on networked filesystems (hundreds of ms each). See the
    # ``atomic_write_json`` docstring for the full tradeoff.
    with contextlib.suppress(OSError):
        _atomic_write_json(cache_path, summary, fsync=False)
    return record
