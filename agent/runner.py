"""Bundled mapreduce + journal operations.

Each public function pairs a cluster-mutating mapreduce primitive with the
corresponding journal update, so slash commands can't accidentally do one
without the other (the failure mode that motivated this module).

``agent.session`` stays pure-IO; this module is the seam where SSH calls
and journal writes meet.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from hpc_mapreduce.infra.remote import run_combiner_checked, ssh_run

from agent import session
from agent.session import RunRecord

__all__ = [
    "submit_and_record",
    "record_status",
    "combine_wave",
    "resubmit_failed",
    "reconcile",
    "mark_terminal",
]


def _split_ssh_target(ssh_target: str) -> tuple[str, str]:
    """Split a ``user@host`` target into ``(user, host)``."""
    if "@" not in ssh_target:
        raise ValueError(f"ssh_target must be 'user@host', got {ssh_target!r}")
    user, host = ssh_target.split("@", 1)
    return user, host


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def submit_and_record(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    manifest_filename: str,
    job_ids: list[str],
    total_tasks: int,
    run_id: str | None = None,
) -> RunRecord:
    """Build a fresh ``RunRecord`` and upsert it to the journal.

    ``run_id`` defaults to ``f"{profile}_{cmd_sha8}"`` where ``cmd_sha8``
    is the prefix of *manifest_filename* (``manifest.<sha8>.json``).
    """
    if run_id is None:
        sha8 = manifest_filename.removeprefix("manifest.").removesuffix(".json")
        run_id = f"{profile}_{sha8}"
    record = RunRecord(
        run_id=run_id,
        profile=profile,
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        job_name=job_name,
        job_ids=list(job_ids),
        manifest=manifest_filename,
        total_tasks=int(total_tasks),
        submitted_at=_utcnow_iso(),
        experiment_dir=str(Path(experiment_dir).resolve()),
    )
    session.upsert_run(experiment_dir, record)
    return record


def _ssh_status_report(
    *,
    ssh_target: str,
    remote_path: str,
    manifest_filename: str,
    job_ids: list[str],
    job_name: str,
    log_dir: str = "logs",
    file_glob: str = "*",
) -> dict:
    """Run the on-cluster status reporter and return its parsed JSON."""
    user, host = _split_ssh_target(ssh_target)
    job_ids_csv = ",".join(job_ids)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"python -m hpc_mapreduce.reduce.status "
        f"--manifest {shlex.quote(manifest_filename)} "
        f"--job-ids {shlex.quote(job_ids_csv)} "
        f"--job-name {shlex.quote(job_name)} "
        f"--log-dir {shlex.quote(log_dir)} "
        f"--file-glob {shlex.quote(file_glob)}"
    )
    proc = ssh_run(cmd, host=host, user=user)
    if proc.returncode != 0:
        raise RuntimeError(
            f"status reporter failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"status reporter returned invalid JSON: {exc}; first 200 chars: "
            f"{proc.stdout[:200]!r}"
        ) from exc


def record_status(
    experiment_dir: Path,
    run_id: str,
    *,
    ssh_target: str,
    remote_path: str,
    manifest_filename: str,
    job_ids: list[str],
    job_name: str,
    file_glob: str = "*",
) -> RunRecord:
    """Run the status reporter and write ``last_status`` to the journal."""
    report = _ssh_status_report(
        ssh_target=ssh_target,
        remote_path=remote_path,
        manifest_filename=manifest_filename,
        job_ids=job_ids,
        job_name=job_name,
        file_glob=file_glob,
    )
    summary = dict(report.get("summary", {}))
    summary["checked_at"] = _utcnow_iso()
    return session.update_run_status(experiment_dir, run_id, last_status=summary)


def combine_wave(
    experiment_dir: Path,
    run_id: str,
    *,
    wave: int,
    ssh_target: str,
    remote_path: str,
    manifest_filename: str = "_hpc_dispatch.json",
    force: bool = False,
) -> tuple[bool, str, str]:
    """Run the on-cluster combiner for *wave*; record the outcome.

    On success, append *wave* to ``combined_waves``. On failure, append
    to ``failed_waves`` and never mark the wave combined. Returns the raw
    ``(ok, stdout, stderr)`` from ``run_combiner_checked``.
    """
    user, host = _split_ssh_target(ssh_target)
    ok, stdout, stderr = run_combiner_checked(
        host=host,
        user=user,
        remote_path=remote_path,
        wave=wave,
        manifest_name=manifest_filename,
        force=force,
    )
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise FileNotFoundError(f"no run record for {run_id!r}")
    if ok:
        if wave not in record.combined_waves:
            record.combined_waves = sorted({*record.combined_waves, wave})
        record.failed_waves = [w for w in record.failed_waves if w != wave]
    else:
        if wave not in record.failed_waves:
            record.failed_waves = sorted({*record.failed_waves, wave})
    session.update_run_status(
        experiment_dir,
        run_id,
        combined_waves=record.combined_waves,
        failed_waves=record.failed_waves,
    )
    return ok, stdout, stderr


def resubmit_failed(
    experiment_dir: Path,
    run_id: str,
    *,
    failed_task_ids: list[int],
    category: str,
    overrides: dict[str, Any] | None = None,
    new_job_ids: list[str] | None = None,
) -> RunRecord:
    """Record a resubmission attempt in the journal.

    The actual resubmit (manifest building + backend submission) is the
    caller's responsibility — this helper only updates per-task retry
    counters and (optionally) the active job_ids list. Pass
    ``new_job_ids`` after the backend reports them so the journal stays
    in sync for the next monitor session.
    """
    if not failed_task_ids:
        raise ValueError("resubmit_failed requires at least one failed task id")
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise FileNotFoundError(f"no run record for {run_id!r}")
    retries = dict(record.retries)
    overrides = dict(overrides or {})
    for tid in failed_task_ids:
        key = str(tid)
        prior = retries.get(key, {})
        retries[key] = {
            "attempts": int(prior.get("attempts", 0)) + 1,
            "category": category,
            "overrides": overrides,
        }
    fields: dict[str, Any] = {"retries": retries}
    if new_job_ids is not None:
        fields["job_ids"] = list(new_job_ids)
    return session.update_run_status(experiment_dir, run_id, **fields)


def _ssh_list_combined_waves(
    *, ssh_target: str, remote_path: str
) -> list[int]:
    """Derive ``combined_waves`` from cluster artifacts.

    The combiner writes ``_combiner/wave_<N>.json`` per successful run
    (see ``hpc_mapreduce/map/combiner.py``). We use the presence of
    that file as the success marker.
    """
    user, host = _split_ssh_target(ssh_target)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        "ls _combiner/wave_*.json 2>/dev/null || true"
    )
    proc = ssh_run(cmd, host=host, user=user)
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
    """Return the subset of *job_ids* still known to the scheduler."""
    if not job_ids:
        return set()
    user, host = _split_ssh_target(ssh_target)
    csv = ",".join(job_ids)
    if scheduler == "slurm":
        cmd = (
            f"squeue -j {shlex.quote(csv)} -h -o '%i' 2>/dev/null; "
            f"sacct -j {shlex.quote(csv)} -n -P -o JobID 2>/dev/null"
        )
    else:  # sge
        cmd = (
            "{ "
            + "; ".join(f"qstat -j {shlex.quote(jid)} 2>/dev/null | head -1 "
                        f"&& echo __ALIVE__{jid}" for jid in job_ids)
            + "; } || true"
        )
    proc = ssh_run(cmd, host=host, user=user)
    alive: set[str] = set()
    for line in proc.stdout.splitlines():
        token = line.strip()
        if not token:
            continue
        if scheduler == "slurm":
            base = token.split(".")[0].split("_")[0]
            if base in job_ids:
                alive.add(base)
        else:
            if token.startswith("__ALIVE__"):
                alive.add(token.removeprefix("__ALIVE__"))
    return alive


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
        raise FileNotFoundError(f"no run record for {run_id!r}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_status = pool.submit(
            _ssh_status_report,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            manifest_filename=record.manifest,
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

        try:
            report = fut_status.result()
            summary = dict(report.get("summary", {}))
        except Exception as exc:
            summary = {"error": str(exc)}
        summary["checked_at"] = _utcnow_iso()

        combined = fut_waves.result()
        alive = fut_alive.result()

    fields: dict[str, Any] = {
        "last_status": summary,
        "combined_waves": combined,
        # Drop any failed_waves entries that are now combined.
        "failed_waves": [w for w in record.failed_waves if w not in set(combined)],
    }
    updated = session.update_run_status(experiment_dir, run_id, **fields)

    if record.job_ids and not alive:
        updated = session.mark_run(
            experiment_dir, run_id, status="abandoned"
        )
    return updated


def mark_terminal(
    experiment_dir: Path,
    run_id: str,
    *,
    status: str,
    stage: str | None = None,
) -> RunRecord:
    """Thin pass-through to ``session.mark_run`` for symmetry."""
    return session.mark_run(experiment_dir, run_id, status=status, stage=stage)
