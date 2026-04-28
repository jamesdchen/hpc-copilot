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
    "record_status",
    "combine_wave",
    "resubmit_failed",
    "reconcile",
    "mark_terminal",
    "verify_per_task_outputs",
    "verify_combiner_artifact",
    "build_provenance",
    "write_remote_provenance",
    "validate_manifest_file",
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

# Manifest filenames are content-addressed: ``manifest.<sha8>.json``
# where ``sha8`` is exactly 8 hex chars.  ``submit_and_record`` derives
# the run_id suffix from this prefix, so a non-conforming filename used
# to silently produce garbage run_ids that violated the
# ``submit.output.json`` schema.
_MANIFEST_NAME_RE = re.compile(r"^manifest\.([0-9a-f]{8})\.json$")


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
) -> tuple[RunRecord, bool]:
    """Build a fresh ``RunRecord`` and upsert it to the journal.

    ``run_id`` defaults to ``f"{profile}_{cmd_sha8}"`` where ``cmd_sha8``
    is the prefix of *manifest_filename* (``manifest.<sha8>.json``).

    Returns ``(record, deduped)`` where ``deduped`` is True if a record
    with this ``run_id`` already existed and the call was a no-op replay.
    Submissions are deterministic in ``run_id`` (profile + manifest sha),
    so an agent that retries on transient network errors gets dedup for
    free — the cluster does not see duplicate ``qsub``/``sbatch`` calls
    because the caller checks the returned ``deduped`` flag before issuing
    them. The bundled CLI uses this to make ``submit`` idempotent.
    """
    if run_id is None:
        match = _MANIFEST_NAME_RE.match(manifest_filename)
        if not match:
            raise errors.ManifestInvalid(
                f"manifest_filename {manifest_filename!r} must match "
                f"'manifest.<8 hex chars>.json'; pass an explicit run_id "
                f"to bypass this check."
            )
        run_id = f"{profile}_{match.group(1)}"

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
        manifest=manifest_filename,
        total_tasks=int(total_tasks),
        submitted_at=_utcnow_iso(),
        experiment_dir=str(Path(experiment_dir).resolve()),
    )
    session.upsert_run(experiment_dir, record)
    return record, False


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
    manifest_filename: str,
    job_ids: list[str],
    job_name: str,
    file_glob: str = "*",
) -> RunRecord:
    """Run the status reporter and write ``last_status`` to the journal.

    Also writes the snapshot to ``<run_id>.last_status.json`` next to the
    journal record so any consumer (agent, human, ``jq`` pipeline, file
    watcher) can read the latest cached state without re-issuing an SSH
    call. The file's mtime tells the caller how stale the snapshot is.
    """
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
        raise errors.JournalCorrupt(f"no run record for {run_id!r}")
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


# ─── pre-submit manifest sanity ─────────────────────────────────────────────


# Schema versions accepted by the on-cluster dispatcher.  Kept in sync
# with ``hpc_mapreduce.map.dispatch.SUPPORTED_SCHEMA_VERSIONS`` and
# ``hpc_mapreduce.job.grid.MANIFEST_SCHEMA_VERSION``.
_SUPPORTED_MANIFEST_VERSIONS = (1, 2)

# Match unresolved ``{placeholder}`` tokens.  False positives are
# minimised by requiring an identifier-like body and a closing brace.
# (Real shell brace expansion uses commas or numeric ranges, which this
# pattern does not match.)
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def validate_manifest_file(manifest_path: Path) -> None:
    """Raise :class:`ManifestInvalid` if *manifest_path* is unsafe to submit.

    Checks (all client-side, no SSH):

    * the file exists and is valid JSON;
    * ``schema_version`` is in :data:`_SUPPORTED_MANIFEST_VERSIONS`;
    * ``tasks`` is a non-empty dict, ``total_tasks`` matches ``len(tasks)``;
    * every task carries ``cmd``, ``result_dir``, ``params`` with the right
      types;
    * no ``{placeholder}`` remnants in rendered ``cmd`` / ``result_dir``;
    * if ``wave_map`` is present, its members exactly cover ``tasks``.

    Catches the entire class of "manifest looked fine locally, crashed
    mid-run on the cluster" failures.
    """
    if not manifest_path.is_file():
        raise errors.ManifestInvalid(
            f"manifest not found at {manifest_path}; rebuild via /submit "
            f"or `hpc-mapreduce submit`."
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise errors.ManifestInvalid(
            f"manifest at {manifest_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise errors.ManifestInvalid(
            f"manifest at {manifest_path} must be a JSON object; "
            f"got {type(manifest).__name__}"
        )
    schema_version = manifest.get("schema_version")
    if schema_version not in _SUPPORTED_MANIFEST_VERSIONS:
        raise errors.ManifestInvalid(
            f"manifest schema_version={schema_version!r}; supported="
            f"{list(_SUPPORTED_MANIFEST_VERSIONS)}. Regenerate with current hpc_mapreduce."
        )

    tasks = manifest.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise errors.ManifestInvalid(
            f"manifest.tasks must be a non-empty object; "
            f"got {type(tasks).__name__ if tasks is None else 'empty'}"
        )

    total_tasks = manifest.get("total_tasks")
    if isinstance(total_tasks, int) and total_tasks != len(tasks):
        raise errors.ManifestInvalid(
            f"manifest.total_tasks ({total_tasks}) does not match "
            f"len(tasks)={len(tasks)} — manifest is inconsistent."
        )

    for tid, task in tasks.items():
        if not isinstance(task, dict):
            raise errors.ManifestInvalid(
                f"manifest.tasks[{tid!r}] must be an object; "
                f"got {type(task).__name__}"
            )
        cmd = task.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise errors.ManifestInvalid(
                f"manifest.tasks[{tid!r}].cmd must be a non-empty string"
            )
        if _PLACEHOLDER_RE.search(cmd):
            leftover = _PLACEHOLDER_RE.findall(cmd)
            raise errors.ManifestInvalid(
                f"manifest.tasks[{tid!r}].cmd has unresolved placeholder(s) "
                f"{leftover}: {cmd!r}. Rebuild the manifest so all "
                f"{{name}} tokens render before submit."
            )
        result_dir = task.get("result_dir")
        if not isinstance(result_dir, str) or not result_dir.strip():
            raise errors.ManifestInvalid(
                f"manifest.tasks[{tid!r}].result_dir must be a non-empty string"
            )
        if _PLACEHOLDER_RE.search(result_dir):
            leftover = _PLACEHOLDER_RE.findall(result_dir)
            raise errors.ManifestInvalid(
                f"manifest.tasks[{tid!r}].result_dir has unresolved "
                f"placeholder(s) {leftover}: {result_dir!r}."
            )
        params = task.get("params")
        if not isinstance(params, dict):
            raise errors.ManifestInvalid(
                f"manifest.tasks[{tid!r}].params must be an object"
            )

    wave_map = manifest.get("wave_map")
    if wave_map is not None:
        if not isinstance(wave_map, dict):
            raise errors.ManifestInvalid(
                f"manifest.wave_map must be an object mapping wave -> [task_ids]"
            )
        covered: set[str] = set()
        for wave_key, members in wave_map.items():
            if not isinstance(members, list):
                raise errors.ManifestInvalid(
                    f"manifest.wave_map[{wave_key!r}] must be a list of task ids"
                )
            for tid in members:
                covered.add(str(tid))
        task_ids = set(tasks.keys())
        missing_from_waves = task_ids - covered
        unknown_in_waves = covered - task_ids
        if missing_from_waves:
            raise errors.ManifestInvalid(
                f"wave_map omits task id(s) {sorted(missing_from_waves)[:10]} — "
                f"every task must belong to a wave."
            )
        if unknown_in_waves:
            raise errors.ManifestInvalid(
                f"wave_map references unknown task id(s) "
                f"{sorted(unknown_in_waves)[:10]} — manifest is inconsistent."
            )


# ─── aggregate preconditions / postconditions / provenance ──────────────────
#
# These helpers are framework-agnostic guarantees around the user-supplied
# combiner.  They check plumbing (every task produced output, the combiner
# wrote what it claimed to write, the aggregated artifact carries provenance
# tied to the run) without learning anything about experiment semantics.
# Both /aggregate and `hpc-mapreduce aggregate` use them.


def _read_remote_manifest(
    *, ssh_target: str, remote_path: str, manifest_filename: str
) -> dict[str, Any]:
    """SSH-cat the dispatch manifest from the cluster and parse it.

    The manifest carries ``wave_map`` (set by ``attach_wave_map``) which
    maps wave index -> list of task ids belonging to that wave.  When
    absent, we fall back to assuming every task belongs to wave 0.
    """
    user, host = _split_ssh_target(ssh_target)
    cmd = f"cat {shlex.quote(f'{remote_path}/{manifest_filename}')}"
    proc = ssh_run(cmd, host=host, user=user)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"failed to read remote manifest at {remote_path}/{manifest_filename}: "
            f"{proc.stderr.strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteCommandFailed(
            f"remote manifest at {remote_path}/{manifest_filename} is not valid JSON: {exc}"
        ) from exc


def _wave_task_ids(manifest: dict[str, Any], wave: int) -> list[str]:
    """Return task ids belonging to *wave* per ``manifest['wave_map']``.

    Falls back to "every task" when ``wave==0`` and no wave_map is present
    (un-batched submissions ship a single implicit wave-0).
    """
    wave_map = manifest.get("wave_map") or {}
    if wave_map:
        members = wave_map.get(str(wave))
        return [str(t) for t in members] if members else []
    if wave == 0:
        return list((manifest.get("tasks") or {}).keys())
    return []


def verify_per_task_outputs(
    *,
    ssh_target: str,
    remote_path: str,
    manifest_filename: str,
    wave: int,
    template: str,
) -> list[str]:
    """Check every per-task output named by *template* exists on the cluster.

    *template* may include ``{task_id}``; it is substituted with each task
    id in the wave (per ``manifest['wave_map']``).  Paths are interpreted
    relative to *remote_path* unless absolute.

    Returns the list of *missing* paths (relative to remote_path or
    absolute as written).  Empty list = all expected outputs are present.
    """
    manifest = _read_remote_manifest(
        ssh_target=ssh_target,
        remote_path=remote_path,
        manifest_filename=manifest_filename,
    )
    task_ids = _wave_task_ids(manifest, wave)
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
        "manifest": record.manifest,
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
