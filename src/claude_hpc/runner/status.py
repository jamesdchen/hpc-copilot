"""Status-reporting runner primitives."""

from __future__ import annotations

import contextlib
import shlex
from typing import TYPE_CHECKING

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc._internal._time import utcnow_iso
from claude_hpc._internal.session import RunRecord, _atomic_write_json
from claude_hpc.errors import RemoteCommandFailed
from claude_hpc.infra import remote
from claude_hpc.runner._ssh import _parse_remote_json

if TYPE_CHECKING:
    from pathlib import Path


def _ssh_status_report(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    job_ids: list[str],
    job_name: str,
    log_dir: str = "logs",
    file_glob: str = "*",
) -> dict:
    """Run the on-cluster status reporter (``--run-id``) and return parsed JSON.

    The reporter reads ``.hpc/runs/<run_id>.json`` for run metadata and
    ``.hpc/tasks.py`` for per-task kwargs, then emits the JSON envelope
    pinned by ``docs/reference/python-api-contract.md`` (summary / tasks / rollup /
    errors).
    """
    job_ids_csv = ",".join(job_ids)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"python -m claude_hpc.mapreduce.reduce.status "
        f"--run-id {shlex.quote(run_id)} "
        f"--job-ids {shlex.quote(job_ids_csv)} "
        f"--job-name {shlex.quote(job_name)} "
        f"--log-dir {shlex.quote(log_dir)} "
        f"--file-glob {shlex.quote(file_glob)}"
    )
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"status reporter failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    return _parse_remote_json(proc.stdout, source_label="status reporter")


# Public alias — atoms / external orchestrators that need to invoke the
# remote status reporter directly should reach for this name. The
# underscore-prefixed original is kept for back-compat with the
# package-internal callers (``reconcile``, ``failures``, ``logs``,
# ``record_status``).
ssh_status_report = _ssh_status_report


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
    cli="hpc-mapreduce status --run-id <id> [--experiment-dir <dir>]",
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
) -> RunRecord:
    """Run the status reporter and write ``last_status`` to the journal.

    The cluster-side reporter reads ``.hpc/runs/<run_id>.json`` for run
    metadata and ``.hpc/tasks.py`` for per-task kwargs.

    Also writes the snapshot to ``<run_id>.last_status.json`` next to the
    journal record so any consumer (agent, human, ``jq`` pipeline, file
    watcher) can read the latest cached state without re-issuing an SSH
    call. The file's mtime tells the caller how stale the snapshot is.
    """
    report = _ssh_status_report(
        ssh_target=ssh_target,
        remote_path=remote_path,
        run_id=run_id,
        job_ids=job_ids,
        job_name=job_name,
        file_glob=file_glob,
    )
    summary = dict(report.get("summary", {}))
    summary["checked_at"] = utcnow_iso()
    # Carry per-wave breakdown into the persisted last_status when the
    # cluster-side reporter emitted one (sidecar carried a wave_map).
    if isinstance(report.get("waves"), dict) and report["waves"]:
        summary["waves"] = report["waves"]
    record = session.update_run_status(experiment_dir, run_id, last_status=summary)
    # Cache the snapshot for cheap external reads. Best-effort: a write
    # failure here must not roll back the journal update.
    cache_path = session.runs_dir(experiment_dir) / f"{run_id}.last_status.json"
    # Atomic write so a concurrent reader never sees a half-written
    # file.  ``Path.write_text`` truncates in place; readers that
    # race with the writer would otherwise observe a JSONDecodeError.
    with contextlib.suppress(OSError):
        _atomic_write_json(cache_path, summary)
    return record
