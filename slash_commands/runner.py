"""Bundled mapreduce + journal operations.

Each public function pairs a cluster-mutating mapreduce primitive with the
corresponding journal update, so slash commands can't accidentally do one
without the other (the failure mode that motivated this module).

``slash_commands.session`` stays pure-IO; this module is the seam where SSH calls
and journal writes meet.
"""

from __future__ import annotations

import json
import re
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from hpc_mapreduce._time import utcnow_iso
from hpc_mapreduce.infra.remote import run_combiner_checked, ssh_run
from slash_commands import errors, session
from slash_commands.errors import RemoteCommandFailed
from slash_commands.session import RunRecord, _atomic_write_json

__all__ = [
    "submit_and_record",
    "build_job_env",
    "record_status",
    "combine_wave",
    "resubmit_failed",
    "reconcile",
    "mark_terminal",
    "verify_per_task_outputs",
    "verify_combiner_artifact",
    "build_provenance",
    "write_remote_provenance",
    "fetch_task_logs",
    "cluster_failures_by_fingerprint",
    "fingerprint_stderr_tail",
    "derive_resubmit_request_id",
    "annotate_clusters_with_retry_advice",
]


def _split_ssh_target(ssh_target: str) -> tuple[str, str]:
    """Split a ``user@host`` target into ``(user, host)``."""
    if "@" not in ssh_target:
        raise ValueError(f"ssh_target must be 'user@host', got {ssh_target!r}")
    user, host = ssh_target.split("@", 1)
    return user, host


# Backwards-compatible alias for tests/external imports that referenced
# the original helper here.  The canonical implementation now lives in
# ``hpc_mapreduce._time`` so timestamps stay consistent across the
# package.
_utcnow_iso = utcnow_iso


def submit_and_record(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    run_id: str,
    job_ids: list[str],
    total_tasks: int,
) -> tuple[RunRecord, bool]:
    """Build a fresh ``RunRecord`` and upsert it to the journal.

    The journal entry is keyed by *run_id* — the per-run sidecar at
    ``.hpc/runs/<run_id>.json`` is the source of truth for everything
    the cluster-side dispatcher and combiner consume; the journal record
    is the laptop-side bookkeeping that lets a future ``/status`` resume
    monitoring without re-asking the user for cluster / job_ids.

    Returns ``(record, deduped)`` where ``deduped`` is True if a record
    with this ``run_id`` already existed and the call was a no-op replay.
    Submissions are deterministic in ``run_id``, so a retry on transient
    network errors gets dedup for free — the cluster does not see
    duplicate ``qsub``/``sbatch`` calls because the caller checks the
    returned ``deduped`` flag before issuing them.
    """
    if not run_id:
        raise errors.ManifestInvalid("submit_and_record requires a non-empty run_id")

    existing = session.load_run(experiment_dir, run_id)
    if existing is not None:
        return existing, True

    record = RunRecord(
        run_id=run_id,
        profile=profile,
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        job_name=job_name,
        job_ids=list(job_ids),
        total_tasks=int(total_tasks),
        submitted_at=_utcnow_iso(),
        experiment_dir=str(Path(experiment_dir).resolve()),
    )
    session.upsert_run(experiment_dir, record)
    return record, False


def build_job_env(
    manifest: dict[str, Any], base_env: dict[str, str]
) -> dict[str, str]:
    """Return *base_env* augmented with runtime-derived env vars.

    Today: when ``manifest.get("runtime") == "uv"``, sets
    ``HPC_RUNTIME=uv`` so the cluster-side template's ``uv sync``
    preamble fires. For any other runtime value (or none), returns a
    plain copy of *base_env*. Never mutates either input.

    Add new branches as new runtime profiles land (``pixi``, ``poetry``,
    …); the contract — copy + augment — should stay invariant.
    """
    env = dict(base_env)
    if manifest.get("runtime") == "uv":
        env["HPC_RUNTIME"] = "uv"
    return env


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
    ``.hpc/tasks.py`` for per-task kwargs, then emits the same JSON
    envelope the legacy ``--manifest`` path produced.
    """
    user, host = _split_ssh_target(ssh_target)
    job_ids_csv = ",".join(job_ids)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"python -m hpc_mapreduce.reduce.status "
        f"--run-id {shlex.quote(run_id)} "
        f"--job-ids {shlex.quote(job_ids_csv)} "
        f"--job-name {shlex.quote(job_name)} "
        f"--log-dir {shlex.quote(log_dir)} "
        f"--file-glob {shlex.quote(file_glob)}"
    )
    proc = ssh_run(cmd, host=host, user=user)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"status reporter failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteCommandFailed(
            f"status reporter returned invalid JSON: {exc}; first 200 chars: "
            f"{proc.stdout[:200]!r}"
        ) from exc


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
    summary["checked_at"] = _utcnow_iso()
    # Carry per-wave breakdown into the persisted last_status when the
    # cluster-side reporter emitted one (manifest had a wave_map).
    if isinstance(report.get("waves"), dict) and report["waves"]:
        summary["waves"] = report["waves"]
    record = session.update_run_status(experiment_dir, run_id, last_status=summary)
    # Cache the snapshot for cheap external reads. Best-effort: a write
    # failure here must not roll back the journal update.
    cache_path = session.runs_dir(experiment_dir) / f"{run_id}.last_status.json"
    try:
        # Atomic write so a concurrent reader never sees a half-written
        # file.  ``Path.write_text`` truncates in place; readers that
        # race with the writer would otherwise observe a JSONDecodeError.
        _atomic_write_json(cache_path, summary)
    except OSError:
        pass
    return record


def combine_wave(
    experiment_dir: Path,
    run_id: str,
    *,
    wave: int,
    ssh_target: str,
    remote_path: str,
    force: bool = False,
) -> tuple[bool, str, str]:
    """Run the on-cluster combiner for *wave*; record the outcome.

    The cluster-side combiner (``.hpc/_hpc_combiner.py``) reads the
    per-run sidecar at ``.hpc/runs/<run_id>.json`` to discover the
    wave_map and result_dir_template. On success, append *wave* to
    ``combined_waves``. On failure, append to ``failed_waves`` and never
    mark the wave combined. Returns ``(ok, stdout, stderr)`` from
    :func:`run_combiner_checked`.
    """
    user, host = _split_ssh_target(ssh_target)
    ok, stdout, stderr = run_combiner_checked(
        host=host,
        user=user,
        remote_path=remote_path,
        wave=wave,
        run_id=run_id,
        force=force,
    )
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}")
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


def derive_resubmit_request_id(
    *,
    failed_task_ids: list[int],
    category: str,
    overrides: dict[str, Any] | None,
) -> str:
    """Compute a deterministic dedupe key from the resubmit spec.

    Same input → same id, regardless of dict-key order in *overrides*.
    First 12 hex chars of sha256, prefixed with ``rs_`` for readability.
    """
    import hashlib

    payload = json.dumps(
        {
            "failed_task_ids": sorted(int(t) for t in failed_task_ids),
            "category": category,
            "overrides": overrides or {},
        },
        sort_keys=True,
    )
    return "rs_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def resubmit_failed(
    experiment_dir: Path,
    run_id: str,
    *,
    failed_task_ids: list[int],
    category: str,
    overrides: dict[str, Any] | None = None,
    new_job_ids: list[str] | None = None,
    request_id: str | None = None,
) -> tuple[RunRecord, bool, str]:
    """Record a resubmission attempt in the journal.

    The actual resubmit (manifest building + backend submission) is the
    caller's responsibility — this helper only updates per-task retry
    counters and (optionally) the active job_ids list. Pass
    ``new_job_ids`` after the backend reports them so the journal stays
    in sync for the next monitor session.

    Idempotent on ``request_id``. When the caller does not supply one,
    a deterministic id is derived from the spec via
    :func:`derive_resubmit_request_id`. A second call with the same
    ``request_id`` (whether explicit or derived) returns
    ``(record, deduped=True, request_id)`` without incrementing
    per-task retry counters.

    Returns ``(record, deduped, request_id)``.
    """
    if not failed_task_ids:
        raise ValueError("resubmit_failed requires at least one failed task id")
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}")

    rid = request_id or derive_resubmit_request_id(
        failed_task_ids=failed_task_ids,
        category=category,
        overrides=overrides,
    )
    if record.last_resubmit_request_id and record.last_resubmit_request_id == rid:
        # Deduped: replay of the same resubmit. Don't increment counters.
        return record, True, rid

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
    fields: dict[str, Any] = {
        "retries": retries,
        "last_resubmit_request_id": rid,
    }
    if new_job_ids is not None:
        fields["job_ids"] = list(new_job_ids)
    updated = session.update_run_status(experiment_dir, run_id, **fields)
    return updated, False, rid


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
    """Return the subset of *job_ids* still known to the scheduler.

    "Alive" means *currently* known to the scheduler (queued, running,
    requeued).  Slurm's ``sacct`` reports historical jobs too — completed,
    cancelled, failed — so we deliberately skip it here; ``squeue``
    alone covers pending+running+requeued, which is what callers actually
    want when deciding whether a run has been abandoned.
    """
    if not job_ids:
        return set()
    user, host = _split_ssh_target(ssh_target)
    csv = ",".join(job_ids)
    if scheduler == "slurm":
        # squeue lists only active states; sacct would leak completed
        # jobs into the alive set and cause runs to never be marked
        # abandoned.
        cmd = f"squeue -j {shlex.quote(csv)} -h -o '%i' 2>/dev/null || true"
    else:  # sge
        # Key the marker on qstat's *exit code*, not on the pipeline
        # tail.  ``qstat | head -1`` would always return 0 (head reads
        # empty stdin successfully), making ``&& echo __ALIVE__`` fire
        # for missing jobs and the alive check meaningless.
        cmd = (
            "{ "
            + "; ".join(
                f"qstat -j {shlex.quote(jid)} >/dev/null 2>&1 "
                f"&& echo __ALIVE__{jid}"
                for jid in job_ids
            )
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
        summary["checked_at"] = _utcnow_iso()
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


# ─── per-task log fetching ──────────────────────────────────────────────────


def fetch_task_logs(
    *,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    job_ids: list[str],
    scheduler: str,
    task_ids: list[int],
    lines: int = 50,
) -> list[dict[str, Any]]:
    """SSH to the cluster and tail each task's stderr log.

    Tries the most recent ``job_id`` first, falls back through earlier
    ones (matching :func:`hpc_mapreduce.reduce.status.get_err_log_paths`
    semantics). Returns one dict per task; missing logs surface as
    ``{"task_id": int, "missing": True}``.

    Path conventions (must stay aligned with the job templates):

    * SGE:    ``<remote_path>/<job_name>.o<job_id>.<task_id>``
    * SLURM:  ``<remote_path>/_hpc_logs/<job_name>_<job_id>_<task_id>.err``
    """
    if not task_ids:
        return []
    user, host = _split_ssh_target(ssh_target)
    out: list[dict[str, Any]] = []
    for tid in task_ids:
        found: dict[str, Any] | None = None
        for job_id in reversed(job_ids or []):
            if scheduler == "sge":
                path = f"{remote_path.rstrip('/')}/{job_name}.o{job_id}.{tid}"
            else:
                path = (
                    f"{remote_path.rstrip('/')}"
                    f"/_hpc_logs/{job_name}_{job_id}_{tid}.err"
                )
            quoted = shlex.quote(path)
            script = (
                f"if [ -f {quoted} ]; then "
                f"echo FOUND; tail -n {int(lines)} {quoted}; "
                f"else echo MISSING; fi"
            )
            proc = ssh_run(script, host=host, user=user)
            if proc.returncode != 0:
                # SSH itself blew up; attribute to this attempt and try
                # the next job_id rather than aborting the whole batch.
                continue
            stdout = proc.stdout or ""
            first, _, rest = stdout.partition("\n")
            if first.strip() == "FOUND":
                found = {
                    "task_id": tid,
                    "path": path,
                    "job_id": job_id,
                    "content": rest,
                }
                break
        if found is None:
            out.append({"task_id": tid, "missing": True})
        else:
            out.append(found)
    return out


# ─── failure clustering by stderr fingerprint ──────────────────────────────


# Patterns that strongly identify a failure category, ordered most-specific first.
# Matched case-insensitively against the joined log tail.  The first hit wins.
_FAILURE_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gpu_oom", re.compile(r"cuda(?: out of memory|.*OOM)|torch\.cuda\.OutOfMemoryError", re.I)),
    ("system_oom", re.compile(r"\boom-killer\b|\bMemoryError\b|killed.*signal 9", re.I)),
    ("walltime", re.compile(r"\bDUE TO TIME LIMIT\b|wall.?time.*exceeded|signal SIGTERM.*15", re.I)),
    ("node_failure", re.compile(r"NODE_FAIL|node failed|connection (closed|reset by peer)|ssh: connect.*refused", re.I)),
    ("import_error", re.compile(r"\bImportError\b|\bModuleNotFoundError\b", re.I)),
    ("file_not_found", re.compile(r"\bFileNotFoundError\b|No such file or directory", re.I)),
    ("permission_denied", re.compile(r"\bPermissionError\b|Permission denied", re.I)),
    ("disk_full", re.compile(r"No space left on device|\bENOSPC\b", re.I)),
    ("python_traceback", re.compile(r"^Traceback \(most recent call last\):", re.I | re.M)),
)

# Lines we strip before fingerprinting so per-task volatility (paths,
# pids, timestamps, line numbers in tracebacks) doesn't fragment a
# single failure mode into many "unique" fingerprints.
_FINGERPRINT_NOISE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*"),  # ISO timestamps
    re.compile(r"\b/(?:home|u|scratch|tmp)/[^\s:]+"),             # absolute paths
    re.compile(r"\bpid[=: ]\d+\b", re.I),
    re.compile(r"\bjob[_ ]?id[=: ]\d+\b", re.I),
    re.compile(r"\btask[_ ]?id[=: ]\d+\b", re.I),
    re.compile(r"\bline \d+"),
    re.compile(r"\b0x[0-9a-fA-F]+\b"),                            # hex pointers
    re.compile(r"\b\d{8,}\b"),                                    # long ints (job ids, pids)
)


def fingerprint_stderr_tail(content: str | None, *, max_chars: int = 400) -> str:
    """Reduce a stderr blob to a stable, comparable fingerprint string.

    Strategy: take the last non-empty line of the tail (typically the
    actual exception), strip volatile noise (timestamps, abs paths, pids,
    hex pointers), and truncate.  Two failures with the same root cause
    on different tasks yield the same fingerprint.
    """
    if not content or not content.strip():
        return ""
    # Last non-empty line: the actual exception is almost always there.
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return ""
    line = lines[-1].strip()
    for pat in _FINGERPRINT_NOISE:
        line = pat.sub("", line)
    # Collapse runs of whitespace introduced by the substitutions.
    line = re.sub(r"\s{2,}", " ", line).strip()
    return line[:max_chars]


def _categorize(content: str | None) -> str:
    """Map a stderr blob to one of :data:`_FAILURE_CATEGORY_PATTERNS` or 'unknown'."""
    if not content:
        return "unknown"
    for category, pat in _FAILURE_CATEGORY_PATTERNS:
        if pat.search(content):
            return category
    return "unknown"


def annotate_clusters_with_retry_advice(
    clusters: list[dict[str, Any]],
    *,
    auto_retry_policy: dict[str, dict[str, Any]],
    record: RunRecord,
) -> list[dict[str, Any]]:
    """Tag each failure cluster with retry eligibility per hpc.yaml policy.

    *auto_retry_policy* is the parsed ``profiles[profile].auto_retry``
    block (see docs/schema.md). Schema:

    .. code-block:: yaml

        auto_retry:
          gpu_oom:        { max_attempts: 1, mem_multiplier: 1.5 }
          system_oom:     { max_attempts: 1, mem_multiplier: 1.5 }
          walltime:       { max_attempts: 1, walltime_multiplier: 2.0 }
          node_failure:   { max_attempts: 2 }

    For each cluster, looks up ``record.retries[tid].attempts`` and tags
    task ids as ``eligible_task_ids`` (attempts < max_attempts) or
    ``blocked_task_ids`` (already at the cap). The policy dict itself is
    echoed back so the caller can compute multiplied overrides.

    Mutates and returns *clusters* for the caller's convenience.
    """
    if not auto_retry_policy:
        return clusters
    for cluster in clusters:
        category = cluster.get("category")
        policy = auto_retry_policy.get(category) if isinstance(category, str) else None
        if not isinstance(policy, dict):
            continue  # No policy for this category; leave untouched.
        max_attempts = int(policy.get("max_attempts", 0) or 0)
        eligible: list[int] = []
        blocked: list[int] = []
        for tid in cluster.get("task_ids", []) or []:
            prior = record.retries.get(str(tid), {}) if record.retries else {}
            attempts = int(prior.get("attempts", 0) or 0)
            if attempts < max_attempts:
                eligible.append(tid)
            else:
                blocked.append(tid)
        cluster["retry_advice"] = {
            "policy": dict(policy),
            "eligible_task_ids": eligible,
            "blocked_task_ids": blocked,
        }
    return clusters


def cluster_failures_by_fingerprint(
    logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group ``fetch_task_logs`` output by failure fingerprint.

    *logs* is the list returned by :func:`fetch_task_logs`.  Output is a
    list of clusters, one per distinct fingerprint, sorted descending by
    member count.  Each cluster carries:

    * ``category``: high-level bucket (gpu_oom, walltime, etc., else 'unknown')
    * ``fingerprint``: noise-stripped last line of the stderr tail
    * ``count``: how many tasks share this failure
    * ``task_ids``: the list of task ids
    * ``sample``: a short representative snippet (last 200 chars)

    Tasks marked ``missing: True`` are bucketed into a single
    ``"log_missing"`` cluster so they're visible in the rollup.
    """
    by_fp: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in logs:
        tid = entry.get("task_id")
        if entry.get("missing"):
            key = ("log_missing", "")
            bucket = by_fp.setdefault(
                key,
                {
                    "category": "log_missing",
                    "fingerprint": "",
                    "count": 0,
                    "task_ids": [],
                    "sample": "",
                },
            )
            bucket["count"] += 1
            if tid is not None:
                bucket["task_ids"].append(tid)
            continue
        content = entry.get("content") or ""
        fp = fingerprint_stderr_tail(content)
        category = _categorize(content)
        key = (category, fp)
        bucket = by_fp.setdefault(
            key,
            {
                "category": category,
                "fingerprint": fp,
                "count": 0,
                "task_ids": [],
                "sample": content[-200:].rstrip(),
            },
        )
        bucket["count"] += 1
        if tid is not None:
            bucket["task_ids"].append(tid)
    clusters = sorted(by_fp.values(), key=lambda b: -b["count"])
    return clusters


# ─── aggregate preconditions / postconditions / provenance ──────────────────
#
# These helpers are framework-agnostic guarantees around the user-supplied
# combiner.  They check plumbing (every task produced output, the combiner
# wrote what it claimed to write, the aggregated artifact carries provenance
# tied to the run) without learning anything about experiment semantics.
# Both /aggregate and `hpc-mapreduce aggregate` use them.


def _read_remote_sidecar(
    *, ssh_target: str, remote_path: str, run_id: str
) -> dict[str, Any]:
    """SSH-cat the per-run sidecar at ``.hpc/runs/<run_id>.json``."""
    user, host = _split_ssh_target(ssh_target)
    sidecar_rel = f".hpc/runs/{run_id}.json"
    cmd = f"cat {shlex.quote(f'{remote_path}/{sidecar_rel}')}"
    proc = ssh_run(cmd, host=host, user=user)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"failed to read remote sidecar at {remote_path}/{sidecar_rel}: "
            f"{proc.stderr.strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteCommandFailed(
            f"remote sidecar at {remote_path}/{sidecar_rel} is not valid JSON: {exc}"
        ) from exc


def _wave_task_ids(sidecar: dict[str, Any], wave: int) -> list[int]:
    """Return task ids belonging to *wave* per ``sidecar['wave_map']``.

    Falls back to "every task" when ``wave==0`` and no wave_map is present
    (un-batched submissions ship a single implicit wave-0).
    """
    wave_map = sidecar.get("wave_map") or {}
    if wave_map:
        members = wave_map.get(str(wave))
        return [int(t) for t in members] if members else []
    if wave == 0:
        return list(range(int(sidecar.get("task_count", 0))))
    return []


def verify_per_task_outputs(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    wave: int,
    template: str,
) -> list[str]:
    """Check every per-task output named by *template* exists on the cluster.

    *template* may include ``{task_id}``; it is substituted with each task
    id in the wave (per the per-run sidecar's ``wave_map``).  Paths are
    interpreted relative to *remote_path* unless absolute.

    Returns the list of *missing* paths (relative to remote_path or
    absolute as written).  Empty list = all expected outputs are present.
    """
    sidecar = _read_remote_sidecar(
        ssh_target=ssh_target,
        remote_path=remote_path,
        run_id=run_id,
    )
    task_ids = _wave_task_ids(sidecar, wave)
    if not task_ids:
        return []
    expected = [template.format(task_id=tid) for tid in task_ids]
    user, host = _split_ssh_target(ssh_target)
    paths_inline = " ".join(shlex.quote(p) for p in expected)
    script = (
        f"cd {shlex.quote(remote_path)} && "
        f"for f in {paths_inline}; do "
        f'[ -f "$f" ] || echo "MISSING:$f"; '
        f"done"
    )
    proc = ssh_run(script, host=host, user=user)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"per-task output existence check failed: "
            f"{proc.stderr.strip()[:500]}"
        )
    return [
        line[len("MISSING:"):].strip()
        for line in proc.stdout.splitlines()
        if line.startswith("MISSING:")
    ]


def verify_combiner_artifact(
    *,
    ssh_target: str,
    remote_path: str,
    expect_output: str,
) -> tuple[bool, str]:
    """Verify the combiner produced *expect_output* (relative to remote_path).

    Existence is always checked.  When the path ends in ``.json`` the file
    is also parsed via ``python3`` on the login node — combiners that exit
    0 but emit truncated/empty JSON don't pass.

    Returns ``(ok, detail)``.  *detail* is "ok" on success or a short
    human-readable reason on failure.
    """
    user, host = _split_ssh_target(ssh_target)
    full_path = f"{remote_path.rstrip('/')}/{expect_output.lstrip('/')}"
    if expect_output.endswith(".json"):
        # python3 -c returns 0 on parse success; non-zero (with stderr) on
        # failure.  Login nodes universally have python3.
        script = (
            f"if [ ! -f {shlex.quote(full_path)} ]; then "
            f"echo MISSING; exit 0; fi; "
            f"python3 -c 'import json,sys; json.load(open({json.dumps(full_path)}))' "
            f"&& echo OK || echo INVALID_JSON"
        )
    else:
        script = (
            f"[ -f {shlex.quote(full_path)} ] && echo OK || echo MISSING"
        )
    proc = ssh_run(script, host=host, user=user)
    out_tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if out_tail == "OK":
        return True, "ok"
    if out_tail == "MISSING":
        return False, f"is missing at {full_path}"
    if out_tail == "INVALID_JSON":
        return False, f"at {full_path} is not valid JSON"
    return False, f"unrecognised verifier output: {proc.stdout.strip()[:200]!r}"


def build_provenance(record: RunRecord, *, wave: int) -> dict[str, Any]:
    """Build the provenance metadata block for an aggregated wave.

    Pure metadata — agnostic to experiment semantics.  Lets a downstream
    consumer (agent or human) verify that an aggregated artifact
    corresponds to the run they expect, without re-querying the journal.
    """
    return {
        "run_id": record.run_id,
        "wave": int(wave),
        "profile": record.profile,
        "cluster": record.cluster,
        "combined_at": utcnow_iso(),
    }


def write_remote_provenance(
    *,
    ssh_target: str,
    remote_path: str,
    expect_output: str,
    provenance: dict[str, Any],
) -> str:
    """Write ``_provenance.json`` next to the combiner's expected output.

    Path resolution: the sidecar lives in the same directory as
    *expect_output* on the cluster.  Returns the absolute remote path
    written.  Best-effort — callers may catch and log; provenance also
    appears in the aggregate envelope so this is a convenience, not a
    contract.
    """
    user, host = _split_ssh_target(ssh_target)
    full_output = f"{remote_path.rstrip('/')}/{expect_output.lstrip('/')}"
    output_dir = full_output.rsplit("/", 1)[0] if "/" in full_output else remote_path
    sidecar = f"{output_dir.rstrip('/')}/_provenance.json"
    payload = json.dumps(provenance, sort_keys=True)
    # Ferry the JSON via base64 to dodge quoting hazards.
    import base64
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    script = (
        f"mkdir -p {shlex.quote(output_dir)} && "
        f"echo {b64} | base64 -d > {shlex.quote(sidecar)}"
    )
    proc = ssh_run(script, host=host, user=user)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"failed to write provenance sidecar at {sidecar}: "
            f"{proc.stderr.strip()[:500]}"
        )
    return sidecar
