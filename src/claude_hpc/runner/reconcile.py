"""Reconcile + mark-terminal runner primitives."""

from __future__ import annotations

import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc._internal._time import utcnow_iso
from claude_hpc.infra import remote
from claude_hpc.runner._ssh import _split_ssh_target
from claude_hpc.runner.status import _ssh_status_report

if TYPE_CHECKING:
    from claude_hpc._internal.session import RunRecord


def _ssh_list_combined_waves(*, ssh_target: str, remote_path: str) -> list[int]:
    """Derive ``combined_waves`` from cluster artifacts.

    The combiner writes ``_combiner/wave_<N>.json`` per successful run
    (see ``claude_hpc/map/combiner.py``). We use the presence of
    that file as the success marker.
    """
    user, host = _split_ssh_target(ssh_target)
    cmd = f"cd {shlex.quote(remote_path)} && ls _combiner/wave_*.json 2>/dev/null || true"
    proc = remote.ssh_run(cmd, host=host, user=user)
    if proc.returncode != 0:
        return []
    waves: set[int] = set()
    for line in proc.stdout.splitlines():
        name = Path(line.strip()).name  # wave_<N>.json
        if not (name.startswith("wave_") and name.endswith(".json")):
            continue
        try:
            waves.add(int(name.removeprefix("wave_").removesuffix(".json")))
        except ValueError:
            continue
    return sorted(waves)


def _ssh_alive_job_ids(
    *, ssh_target: str, remote_path: str, job_ids: list[str], scheduler: str
) -> set[str]:
    """Return the subset of *job_ids* still known to the scheduler.

    "Alive" means *currently* known to the scheduler (queued, running,
    requeued).  Slurm's ``sacct`` reports historical jobs too — completed,
    cancelled, failed — so we deliberately skip it here; ``squeue``
    alone covers pending+running+requeued, which is what callers actually
    want when deciding whether a run has been abandoned.

    B5-PR2: the per-scheduler shell-command shape and the per-scheduler
    output parser both live on the backend class
    (``build_alive_check_cmd`` / ``parse_alive_output``); this function
    is now transport (SSH) only.
    """
    if not job_ids:
        return set()
    from claude_hpc.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    user, host = _split_ssh_target(ssh_target)
    cmd = backend_cls.build_alive_check_cmd(job_ids)
    proc = remote.ssh_run(cmd, host=host, user=user)
    return backend_cls.parse_alive_output(proc.stdout, job_ids)


def reconcile(
    experiment_dir: Path,
    run_id: str,
    *,
    scheduler: str,
    file_glob: str = "*",
) -> RunRecord:
    """Self-healing resume step.

    Re-derives ground truth from the cluster:
      A. Fresh status report -> ``last_status``.
      B. List ``_combiner/wave_*/_combined.ok`` -> canonical
         ``combined_waves`` (cluster wins; journal overwritten on drift).
      C. Cross-check ``job_ids`` against the scheduler; if zero are alive,
         flip ``status`` to ``"abandoned"``.

    All three SSH calls run concurrently. Writes the reconciled record
    back atomically and returns it.
    """
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_status = pool.submit(
            _ssh_status_report,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            run_id=run_id,
            job_ids=record.job_ids,
            job_name=record.job_name,
            file_glob=file_glob,
        )
        fut_waves = pool.submit(
            _ssh_list_combined_waves,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
        )
        fut_alive = pool.submit(
            _ssh_alive_job_ids,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            job_ids=record.job_ids,
            scheduler=scheduler,
        )

        warnings: list[str] = []
        report: dict[str, Any] = {}
        try:
            report = fut_status.result()
            summary = dict(report.get("summary", {}))
        except Exception as exc:
            summary = {"error": str(exc)}
        summary["checked_at"] = utcnow_iso()
        if isinstance(report.get("waves"), dict) and report["waves"]:
            summary["waves"] = report["waves"]

        # Each future has its own try/except: an SSH blip on any of them
        # must not abort the journal update.  In particular, falling
        # back to the *current* job_ids on the alive-check path is
        # essential — defaulting to empty would mark a healthy run
        # ``abandoned`` whenever the SSH check itself failed.
        try:
            combined = fut_waves.result()
        except Exception as exc:
            combined = list(record.combined_waves)
            warnings.append(f"wave list: {exc}")
            alive_check_failed = False
        else:
            alive_check_failed = False

        try:
            alive: list[str] | set[str] = fut_alive.result()
        except Exception as exc:
            alive = list(record.job_ids)  # treat as still alive on error
            warnings.append(f"alive check: {exc}")
            alive_check_failed = True

    if warnings:
        summary["warnings"] = warnings

    fields: dict[str, Any] = {
        "last_status": summary,
        "combined_waves": combined,
        # Drop any failed_waves entries that are now combined.
        "failed_waves": [w for w in record.failed_waves if w not in set(combined)],
    }
    updated = session.update_run_status(experiment_dir, run_id, **fields)

    # Only mark abandoned when the alive check actually ran and found
    # nothing — never on SSH failure of the alive check itself.
    if record.job_ids and not alive and not alive_check_failed:
        updated = session.mark_run(experiment_dir, run_id, status="abandoned")
    return updated


@primitive(
    name="mark-run-terminal",
    verb="mutate",
    side_effects=[
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)",
        ),
    ],
    error_codes=[errors.JournalCorrupt],
    idempotent=True,
    idempotency_key="run_id",
)
def mark_terminal(
    experiment_dir: Path,
    run_id: str,
    *,
    status: str,
    stage: str | None = None,
) -> RunRecord:
    """Thin pass-through to ``session.mark_run`` for symmetry."""
    return session.mark_run(experiment_dir, run_id, status=status, stage=stage)
