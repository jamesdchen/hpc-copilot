"""Job status checking, result validation, and status reporting.

This module drives the LLM-orchestrator's ``/status`` loop.  The CLI entry
point (``python -m hpc_agent.execution.mapreduce.reduce.status --run-id <id>``) emits JSON
to stdout.  **Schema contract** (pinned; all four top-level keys ALWAYS
present, never ``None``)::

    {
        "summary": {"complete": int, "running": int, "pending": int,
                    "failed": int, "unknown": int},
        "tasks": {task_id: {"status": str, "cmd_sha": str | null, ...}, ...},
        "rollup": {grid_key: {"complete": int, "running": int, "pending": int,
                              "failed": int, "unknown": int, "total": int}, ...},
        "errors": [{"code": str, "detail": str}, ...],
    }

The CLI reads ``.hpc/runs/<run_id>.json`` for the run sidecar and
``.hpc/tasks.py`` for the per-task kwargs, then synthesizes a per-task
dict that the reporting helpers consume.  ``tasks[tid].cmd_sha`` is
``null`` in the new model — ``cmd_sha`` lives at the run level.
Additional top-level keys (``total_tasks``, ``scheduler``,
``timestamp``, ``result_dir``, ``err_log_paths``, ``resource_usage``)
may appear but are informational only; the four keys above are the
parse contract.

``resource_usage`` is additive and shaped like::

    {"cpu_hours": float, "gpu_hours": float,
     "elapsed_hours": float, "tasks_counted": int}

Values are summed across all tasks in the status report (not just
completed ones) using whatever the scheduler has reported so far.
"""

from __future__ import annotations

__all__ = [
    "_grid_point_key",
    "check_results",
    "check_results_from_tasks",
    "detect_scheduler",
    "get_err_log_paths",
    "pin_scheduler_profile",
    "report_status",
    "report_status_from_tasks",
    "resolve_scheduler_profile",
    "rollup_by_grid_point",
    "rollup_by_wave",
]

import glob
import json
import os
import subprocess
from pathlib import Path

from hpc_agent._kernel.contract.task_id import HpcTaskId, to_array_index
from hpc_agent._kernel.contract.vocabulary import TaskStatus
from hpc_agent.execution.mapreduce.reduce.rollup import (
    _grid_point_key,
    rollup_by_grid_point,
    rollup_by_wave,
)
from hpc_agent.infra.time import utcnow_iso

# ---------------------------------------------------------------------------
# Result checking
# ---------------------------------------------------------------------------


def check_results(
    result_dir: str | Path,
    total_tasks: int,
    file_glob: str = "*.csv",
    validate: bool = True,
    *,
    min_rows: int = 0,
) -> dict[int, dict]:
    """Scan *result_dir* for completed result files.

    Looks for result files matching *file_glob* in per-task subdirectories
    or directly in *result_dir*.  Returns a dict mapping **0-based**
    ``HpcTaskId`` to status info (the same domain space the dispatcher's
    ``HPC_TASK_ID`` and ``result_dir_template`` ``task_{task_id}`` use).

    A CSV is considered complete when it exists and is non-zero byte (i.e. at
    least a header has been written).  Pass ``min_rows > 0`` to additionally
    require that many data rows beyond the header - useful for tasks where an
    empty result is genuinely a failure.  When ``min_rows == 0`` (the default),
    legitimately-empty outputs (e.g. zero-result CSVs) still count
    as complete and will not trigger auto-resubmit.
    """
    import csv

    results: dict[int, dict] = {}
    rdir = Path(result_dir).resolve()

    def _accept_csv(path_str: str) -> dict | None:
        """Return status dict for a CSV path, or None if it fails the check."""
        try:
            if os.path.getsize(path_str) <= 0:
                return None
            if min_rows <= 0:
                return {"status": "complete", "path": path_str}
            with open(path_str, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    return None
                row_count = sum(1 for _ in reader)
                if row_count < min_rows:
                    return None
            return {"status": "complete", "path": path_str, "csv_rows": row_count}
        except OSError:
            return None

    # Strategy 1: check per-task subdirectories (task_0/, task_1/, ...).
    # Dir index is the 0-based HpcTaskId (the result_dir_template renders
    # ``task_{task_id}`` against the dispatcher's 0-based HPC_TASK_ID).
    for tid in range(total_tasks):
        task_dir = rdir / f"task_{tid}"
        if task_dir.is_dir():
            for path_str in sorted(glob.glob(str(task_dir / file_glob))):
                if "/_wip_" in path_str:
                    continue
                if validate and path_str.endswith(".csv"):
                    status = _accept_csv(path_str)
                    if status is None:
                        continue
                    results[tid] = status
                else:
                    results[tid] = {"status": "complete", "path": path_str}
                break  # one match per task is enough

    # Strategy 2: fall back to flat directory scan if no task subdirs found.
    # With no task_N/ subdirs the only signal for task identity is sorted
    # position, so the glob is sorted for determinism across OS /
    # filesystem implementations. Task ids are assigned by 0-based
    # position (not ``len(results)``): a file that fails the CSV
    # check then leaves its task id absent rather than shifting every
    # later file onto an earlier task's id. ``_wip_`` temp files are
    # dropped first so they don't consume a position slot.
    if not results:
        candidates = [p for p in sorted(glob.glob(str(rdir / file_glob))) if "/_wip_" not in p]
        for tid, path_str in enumerate(candidates[:total_tasks], start=0):
            if validate and path_str.endswith(".csv"):
                status = _accept_csv(path_str)
                if status is None:
                    continue
                results[tid] = status
            else:
                results[tid] = {"status": "complete", "path": path_str}

    return results


# ---------------------------------------------------------------------------
# Scheduler detection
# ---------------------------------------------------------------------------


def detect_scheduler(result_dir: str | Path | None = None) -> str:
    """Auto-detect scheduler type.

    When *result_dir* is given, look for ``experiment_meta.json`` in that
    directory and any of its ancestors up to the filesystem root. This
    matches both the "one shared meta file per experiment" layout (meta
    lives at the experiment root) and the "meta file per task" layout
    (meta lives directly in result_dir).
    """
    if result_dir is not None:
        candidate: Path | None = Path(result_dir)
        seen: set[Path] = set()
        while candidate is not None and candidate not in seen:
            seen.add(candidate)
            meta_path = candidate / "experiment_meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    backend = meta.get("backend", "")
                    # Check the PBS forks before the bare-family names: a
                    # ``pbspro`` hint contains neither "sge" nor "slurm", and
                    # ``torque`` is a distinct PBS fork. Order pbspro/torque
                    # first so a future fork-qualified hint isn't shadowed.
                    if "pbspro" in backend:
                        return "pbspro"
                    if "torque" in backend:
                        return "torque"
                    if "sge" in backend:
                        return "sge"
                    if "slurm" in backend:
                        return "slurm"
                except (json.JSONDecodeError, OSError):
                    pass
                break  # found meta, but its backend was unrecognised
            parent = candidate.parent
            candidate = parent if parent != candidate else None
    try:
        result = subprocess.run(
            ["sacct", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode == 0:
            return "slurm"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "sge"


# ---------------------------------------------------------------------------
# Scheduler-profile RESOLVER (Phase 3)
#
# ``detect_scheduler`` answers "which family?" (a string). The resolver
# below answers the richer question "give me the concrete, validated,
# REGISTERED SchedulerProfile to drive this run, and PIN it so every
# later reader agrees". It seeds from the spine's golden profiles, honours
# an operator's pinned ``scheduler_profile`` from clusters.yaml, registers
# the result via the backend registry, and (for an unknown family) defines
# the LLM-authoring + canary-validate seam.
#
# The spine module ``hpc_agent.infra.backends.profile`` (SchedulerProfile,
# SLURM_PROFILE, SGE_PROFILE) and ``register_profile`` may not exist yet
# during the parallel refactor; everything here imports them lazily and
# raises a clear error only on the paths that actually need them.
# ---------------------------------------------------------------------------

_KNOWN_SCHEDULER_FAMILIES = frozenset({"slurm", "sge", "pbspro", "torque"})


def _golden_profile_for_family(family: str):
    """Return the spine's golden ``SchedulerProfile`` for a known family.

    ``family`` is one of :data:`_KNOWN_SCHEDULER_FAMILIES`
    (slurm / sge / pbspro / torque). Imports the spine lazily so this module
    stays importable before the spine lands. Raises ``NotImplementedError``
    if the spine isn't present yet (this path is only reached for a known
    family, where a golden profile is expected to exist), and ``ValueError``
    for an unrecognised family.
    """
    try:
        from hpc_agent.infra.backends.profile import (
            PBSPRO_PROFILE,
            SGE_PROFILE,
            SLURM_PROFILE,
            TORQUE_PROFILE,
        )
    except ImportError as exc:  # pragma: no cover — spine not present yet
        raise NotImplementedError(
            "spine module hpc_agent.infra.backends.profile is not available "
            "yet; golden profiles (SLURM/SGE/PBSPRO/TORQUE) are required "
            "to resolve a known scheduler family."
        ) from exc
    golden = {
        "slurm": SLURM_PROFILE,
        "sge": SGE_PROFILE,
        "pbspro": PBSPRO_PROFILE,
        "torque": TORQUE_PROFILE,
    }
    fam = family.strip().lower()
    try:
        return golden[fam]
    except KeyError:
        raise ValueError(f"no golden profile for scheduler family {family!r}") from None


def _register(profile):
    """Register *profile* with the backend registry (idempotent).

    Thin lazy wrapper over the spine's ``register_profile`` so callers in
    this module don't repeat the import dance. ``register_profile`` keys on
    ``profile.name`` and is documented as idempotent, so calling it again
    for an already-registered profile is a no-op rebind. Returns the
    backend class ``register_profile`` produced, or ``None`` if the spine
    isn't present yet.
    """
    try:
        from hpc_agent.infra.backends import register_profile
    except ImportError:  # pragma: no cover — spine not present yet
        # TODO(Phase-3): spine's register_profile not present during the
        # parallel refactor. Skip registration; the deterministic resolve +
        # pin still works so downstream readers can re-resolve later.
        return None
    return register_profile(profile, remote=True)


def pin_scheduler_profile(meta_path: str | Path, profile) -> None:
    """PIN a resolved ``SchedulerProfile`` into ``experiment_meta.json``.

    Writes ``profile.to_dict()`` under the top-level key
    ``"scheduler_profile"`` in the ``experiment_meta.json`` at *meta_path*,
    merging into any existing content (so the ``backend`` hint that
    ``detect_scheduler`` reads is preserved). This is the durable record
    that makes the resolved profile authoritative for every later reader of
    the experiment — status, recovery, aggregation — instead of each one
    re-detecting and possibly disagreeing.

    *meta_path* may point either at the ``experiment_meta.json`` file
    itself or at the directory that should contain it; a directory is
    joined with the canonical filename. The parent directory is created if
    needed. A malformed pre-existing file is treated as empty rather than
    crashing the pin.

    NOTE (ownership): nothing in this package currently *writes*
    ``experiment_meta.json`` — it is only read (by ``detect_scheduler``).
    The experiment-setup owner that materialises that file must call this
    helper (or write the ``scheduler_profile`` key itself) right after it
    resolves the profile. See the resolver docstring and the agent report.
    """
    p = Path(meta_path)
    if p.is_dir() or (not p.suffix and not p.exists()):
        p = p / "experiment_meta.json"
    existing: dict = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8")) or {}
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing["scheduler_profile"] = profile.to_dict()
    # Keep the legacy ``backend`` family hint in sync so the cheap
    # detect_scheduler substring path still agrees with the pinned profile.
    family = getattr(profile, "family", None) or getattr(profile, "name", None)
    if isinstance(family, str) and family:
        existing.setdefault("backend", family)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")


def _read_pinned_profile_dict(result_dir: str | Path | None) -> dict | None:
    """Return a previously-pinned ``scheduler_profile`` dict, if any.

    Walks *result_dir* and its ancestors for ``experiment_meta.json`` (the
    same search ``detect_scheduler`` uses) and returns the stored
    ``scheduler_profile`` mapping, or ``None`` when absent/unreadable.
    """
    if result_dir is None:
        return None
    candidate: Path | None = Path(result_dir)
    seen: set[Path] = set()
    while candidate is not None and candidate not in seen:
        seen.add(candidate)
        meta_path = candidate / "experiment_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                pinned = meta.get("scheduler_profile")
                if isinstance(pinned, dict):
                    return pinned
            except (json.JSONDecodeError, OSError):
                pass
            return None
        parent = candidate.parent
        candidate = parent if parent != candidate else None
    return None


def _author_profile_for_unknown_family(
    scheduler: str,
    *,
    cfg: dict | None = None,
    result_dir: str | Path | None = None,
    probe=None,
    llm=None,
):
    """An unrecognised scheduler family has no curated grammar — fail loudly.

    The framework ships curated profiles for slurm / sge / pbspro / torque,
    selected deterministically by detection. A scheduler outside those
    families is NOT auto-authored at runtime: a synthesised profile would
    have no fast, reliable verifier, and the curated families already cover
    the common ground. The supported escape hatches are *data* (pin a
    ``scheduler_profile`` in clusters.yaml) or *code* (add a curated family
    + engine grammar). The ``probe``/``llm`` parameters are retained for
    signature back-compat but are unused.
    """
    from hpc_agent import errors as _errors

    raise _errors.SpecInvalid(
        f"scheduler {scheduler!r} is not a known family "
        "(slurm/sge/pbspro/torque) and no 'scheduler_profile' is pinned. "
        "Pin a SchedulerProfile dict in clusters.yaml, or add a curated "
        "family (engine grammar) — unknown schedulers are not auto-authored."
    )


def _is_golden_profile(profile) -> bool:
    """True when *profile* is just the unmodified golden default for its family.

    A standard slurm/sge cluster resolves to the golden profile, which has
    nothing worth persisting — ``detect_scheduler`` already agrees via the
    cheap ``backend`` hint, and writing the golden script bodies would bloat
    ``clusters.yaml`` / ``experiment_meta.json``. Only *custom* profiles get
    pinned.
    """
    try:
        return bool(profile == _golden_profile_for_family(profile.family))
    except (ValueError, NotImplementedError, AttributeError):
        return False


def _pin_resolved(profile, *, result_dir, cluster_name) -> None:
    """Persist a freshly-resolved CUSTOM profile per the unified rule.

    Always pins to ``experiment_meta.json`` under *result_dir* (the per-run
    source of truth that recover/status read); ADDITIONALLY caches it into a
    writable ``clusters.yaml`` entry when *cluster_name* is given (so the
    next experiment on that cluster skips re-resolution). Both writes are
    best-effort. Golden (unmodified) profiles are skipped — there is nothing
    custom to record.
    """
    import contextlib

    if _is_golden_profile(profile):
        return
    if result_dir is not None:
        with contextlib.suppress(OSError):
            pin_scheduler_profile(result_dir, profile)
    if cluster_name:
        try:
            from hpc_agent.infra.clusters import write_back_scheduler_profile

            write_back_scheduler_profile(cluster_name, profile.to_dict())
        except Exception:  # noqa: BLE001 — caching is strictly best-effort
            pass


def resolve_scheduler_profile(
    scheduler: str,
    *,
    cfg: dict | None = None,
    result_dir: str | Path | None = None,
    cluster_name: str | None = None,
    probe=None,
    llm=None,
):
    """Resolve, register, and (when possible) PIN a ``SchedulerProfile``.

    The single entry point Phase 3 exposes for turning a scheduler name +
    cluster config into the concrete profile that drives a run. Resolution
    order:

    1. **Operator pin in cfg** — if *cfg* carries a ``scheduler_profile``
       dict (a clusters.yaml entry, already round-trip-validated by
       ``ClusterConfig``), build it via ``SchedulerProfile.from_dict``,
       register it, and return it. This wins over the golden family
       profile so an operator can override/augment the defaults. NO LLM.
    2. **Previously pinned in experiment_meta.json** — if no cfg pin but a
       prior resolve wrote a ``scheduler_profile`` under *result_dir*,
       rehydrate it via ``from_dict`` + register + return. Keeps every
       later reader agreeing with the first resolve. NO LLM.
    3. **Known family** (``slurm``/``sge``) with no pin — return the
       spine's golden profile and register it (idempotent). NO LLM.
    4. **Unknown family** with no pin — raises ``SpecInvalid``. Unknown
       schedulers are NOT auto-authored at runtime; the escape hatches are
       *data* (pin a ``scheduler_profile`` in clusters.yaml) or *code* (add
       a curated family). See ``_author_profile_for_unknown_family``.

    When a profile is resolved by a non-pinned path (3 or 4), the unified
    pin rule applies: it is ALWAYS written to ``experiment_meta.json`` under
    *result_dir* (the per-run source of truth recover/status read), and
    ADDITIONALLY cached into a writable ``clusters.yaml`` entry when
    *cluster_name* is given (so the next experiment skips re-resolution).
    Both writes are best-effort.

    Args:
      scheduler: the scheduler family/name (e.g. ``"slurm"``).
      cfg: the cluster config dict for this run (may carry the pin).
      result_dir: a per-run/per-task dir used to locate (and write)
        ``experiment_meta.json`` for the pin.
      cluster_name: the clusters.yaml key for this cluster; when set, the
        resolved profile is cached back into the writable clusters.yaml.
      probe: optional callable for live binary detection (unknown-family
        seam only).
      llm: optional LLM handle for profile authoring (unknown-family seam
        only).

    Returns:
      The resolved ``SchedulerProfile`` (registered under its ``name``).
    """
    cfg = cfg or {}
    fam = (scheduler or "").strip().lower()

    # 1. Operator pin in cfg wins outright.
    cfg_pin = cfg.get("scheduler_profile")
    if isinstance(cfg_pin, dict) and cfg_pin:
        from hpc_agent.infra.backends.profile import SchedulerProfile

        profile = SchedulerProfile.from_dict(cfg_pin)
        _register(profile)
        return profile

    # 2. Previously pinned in experiment_meta.json (durable agreement).
    meta_pin = _read_pinned_profile_dict(result_dir)
    if isinstance(meta_pin, dict) and meta_pin:
        from hpc_agent.infra.backends.profile import SchedulerProfile

        profile = SchedulerProfile.from_dict(meta_pin)
        _register(profile)
        return profile

    # 3. Known family with no pin → deterministic golden seed.
    if fam in _KNOWN_SCHEDULER_FAMILIES:
        profile = _golden_profile_for_family(fam)
        _register(profile)
        _pin_resolved(profile, result_dir=result_dir, cluster_name=cluster_name)
        return profile

    # 4. Unknown family → live LLM-authoring + canary seam, then pin.
    profile = _author_profile_for_unknown_family(
        scheduler, cfg=cfg, result_dir=result_dir, probe=probe, llm=llm
    )
    _pin_resolved(profile, result_dir=result_dir, cluster_name=cluster_name)
    return profile


# ---------------------------------------------------------------------------
# Error log paths
# ---------------------------------------------------------------------------


def get_err_log_paths(
    job_ids: list[str],
    total_tasks: int,
    scheduler: str = "slurm",
    log_dir: str = "",
    job_name: str = "",
    scratch_dir: str = "",
) -> dict[int, str]:
    """Find the most recent error log path on disk for each task.

    Keyed by **0-based** ``HpcTaskId``. The on-disk filename, however, is
    indexed by the scheduler's 1-based ``ArrayIndex`` (logs are named by
    ``%a`` / ``SGE_TASK_ID``), so each id is mapped through
    :func:`~hpc_agent._kernel.contract.task_id.to_array_index` — the single
    validated ``±1`` — before building the path.

    B5-PR2: per-scheduler base path goes through
    :meth:`HPCBackend.err_log_disk_path`. The SLURM fallback glob (which
    catches submission scripts that override ``--error`` to a non-canonical
    name) stays here because it's an on-disk recovery pattern, not a
    scheduler shape question.
    """
    from hpc_agent.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    paths: dict[int, str] = {}
    for tid in range(total_tasks):
        # Submit edge: the log filename carries the 1-based ArrayIndex.
        array_idx = int(to_array_index(HpcTaskId(tid)))
        for job_id in reversed(job_ids):
            p = backend_cls.err_log_disk_path(log_dir, scratch_dir, job_name, job_id, array_idx)
            if scheduler != "sge" and not os.path.isfile(p):
                # Anchor the job_id boundary with a non-digit prefix so
                # the glob can't match a sibling job whose digits happen
                # to end with the requested ``<job_id>_<idx>.err`` slug
                # (e.g. idx=1 + job_id=4 matching ``…14_1.err``).
                matches = glob.glob(os.path.join(log_dir, f"*[!0-9]{job_id}_{array_idx}.err"))
                if matches:
                    p = max(matches, key=os.path.getmtime)
            if os.path.isfile(p):
                paths[tid] = p
                break
    return paths


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------

_ACTIVE_STATES = {"RUNNING", "REQUEUED", "CONFIGURING"}
_PENDING_STATES = {"PENDING"}
_FAILED_STATES = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"}

# Scheduler-terminal SUCCESS states: the accounting record (SGE ``qacct``
# ``exit_status 0``/``failed 0``, sacct ``COMPLETED``, PBS ``Exit_status`` 0 —
# every query parser normalizes these to "COMPLETED") proves the task ran to
# exit 0. Deliberately NOT in the ``_categorize`` map: with the result dir
# intact, completion is defined by the result file on disk, and a COMPLETED
# task with no result stays ``unknown`` (it ran but wrote nothing — including
# the min-rows demotion path). Only the vanished-workdir fallback below
# (:func:`_accounting_complete`) reads this set.
_TERMINAL_SUCCESS_STATES = {"COMPLETED"}


def _empty_summary() -> dict[str, int]:
    """Return the canonical zeroed summary dict (5 int keys, always present).

    Keys derived from :class:`hpc_agent._kernel.contract.vocabulary.TaskStatus` (B2).
    """
    return {ts.value: 0 for ts in TaskStatus}


def _categorize(state: str) -> str:
    """Map a scheduler state string to a summary bucket name (TaskStatus value)."""
    if state in _ACTIVE_STATES:
        return TaskStatus.RUNNING
    if state in _PENDING_STATES:
        return TaskStatus.PENDING
    if state in _FAILED_STATES or state.startswith("CANCELLED"):
        return TaskStatus.FAILED
    return TaskStatus.UNKNOWN


def _accounting_complete(state: str, *, result_dir_vanished: bool) -> bool:
    """True when the scheduler's accounting record is the completion evidence.

    Vanished-workdir fallback (proving run #3, finding f): when a run's
    result location is GONE (mid-run cleanup, scratch purge) no task can ever
    show a result file, so the reporter used to bucket every scheduler-terminal
    task as ``unknown`` and the monitor polled that unknown forever. In that
    one situation the scheduler's terminal accounting record (``qacct``
    ``exit_status 0`` → "COMPLETED") is the only remaining completion evidence,
    and it is positive evidence — the task reached the cluster, ran, and exited
    zero.

    Fires only when BOTH hold:

    * ``result_dir_vanished`` — the task's expected result location no longer
      exists. With the dir intact, a COMPLETED task with no result file stays
      ``unknown`` (ran to exit 0 but wrote nothing, or was demoted by
      ``min_rows``) so an on-disk anomaly is never masked by the exit code;
      the poll loop's bounded-unknown escalation surfaces it instead.
    * the scheduler reports a terminal-success state
      (:data:`_TERMINAL_SUCCESS_STATES`). Terminal *failure* states already
      classify via :func:`_categorize`; absent accounting stays ``unknown``.
    """
    return result_dir_vanished and str(state or "").strip().upper() in _TERMINAL_SUCCESS_STATES


# Dispatcher exit codes that mean "the scheduler bumped this task" — its
# SIGTERM trap exits 130 (128+SIGINT) on preemption / walltime; 143
# (128+SIGTERM) covers an untrapped SIGTERM kill. Single-sourced in spirit
# with the failure-signatures catalog's preempted entry so this fresh,
# per-poll scheduler signal agrees with the log-fingerprint classifier.
_PREEMPT_EXIT_CODES = frozenset({130, 143})


def _is_preempted_task(info: dict) -> bool:
    """True when a per-task scheduler record indicates a preemption.

    Fresh by construction — it reads the *current* attempt's scheduler
    ``exit_code`` (sacct ``ExitCode`` ``"130:0"`` / PBS ``Exit_status`` /
    SGE qacct ``exit_status``) and ``state`` (SLURM ``PREEMPTED``), NOT the
    never-cleared sidecar ``preempt`` mark — so a task that was preempted,
    resumed, then OOM-killed reads exit 137 here and is correctly excluded.
    """
    if not isinstance(info, dict):
        return False
    state = str(info.get("state") or "").strip().upper()
    if state == "PREEMPTED":
        return True
    raw = info.get("exit_code")
    if raw is None:
        return False
    # exit_code arrives as "130:0" (sacct), "130" (pbs/sge), etc. Parse the
    # leading integer before any ':' separator; ignore anything unparseable.
    head = str(raw).split(":", 1)[0].strip()
    try:
        return int(head) in _PREEMPT_EXIT_CODES
    except (TypeError, ValueError):
        return False


def _preempted_ids_from_tasks(tasks: dict) -> list[int]:
    """Sorted 0-based ``HpcTaskId`` task ids whose scheduler record reads preempted.

    Ids are in the domain space (``report_status*`` now keys tasks by 0-based
    ``HPC_TASK_ID``, the conversion having happened at the query ingest edge),
    so consumers — the auto-resume composite, ``resubmit_flow``, the status
    CLI — feed them straight through with no compensating shift.
    """
    out: list[int] = []
    for tid_str, info in (tasks or {}).items():
        if _is_preempted_task(info):
            try:
                out.append(int(tid_str))
            except (TypeError, ValueError):
                continue
    return sorted(out)


def report_status(
    result_dir: str | Path,
    job_ids: list[str],
    total_tasks: int,
    scheduler: str | None = None,
    *,
    file_glob: str = "*.csv",
    log_dir: str = "",
    scratch_dir: str = "",
    job_name: str = "",
    slurm_cluster: str | None = None,
    sge_user: str | None = None,
    min_rows: int = 0,
) -> dict:
    """Assemble a full JSON status report.

    ``min_rows`` is forwarded to :func:`check_results`; see its docstring for the
    CSV completion semantics.
    """
    # B5-PR2: per-scheduler job-state query goes through backend.query_jobs.
    from hpc_agent.infra.backends import get_backend_class

    csv_results = check_results(result_dir, total_tasks, file_glob=file_glob, min_rows=min_rows)

    if scheduler is None:
        scheduler = detect_scheduler(result_dir)

    errors: list[dict] = []
    if job_ids:
        query_result = get_backend_class(scheduler).query_jobs(
            job_ids, sge_user=sge_user, slurm_cluster=slurm_cluster
        )
        job_info = query_result.get("tasks", {}) or {}
        errors.extend(query_result.get("errors", []) or [])
    else:
        job_info = {}

    complete_ids = set(csv_results)
    tasks: dict[str, dict] = {}
    summary = _empty_summary()

    # Vanished-workdir signal: the shared result root itself is gone, so no
    # task can ever produce a result file — see ``_accounting_complete``.
    result_root_vanished = not Path(result_dir).is_dir()

    # 0-based HpcTaskId throughout: csv_results, job_info (query ingest edge),
    # and the keys we emit all speak the domain space.
    for tid in range(total_tasks):
        if tid in complete_ids:
            tasks[str(tid)] = csv_results[tid]
            summary["complete"] += 1
        elif tid in job_info:
            info = job_info[tid]
            state = info["state"]
            cat = _categorize(state)
            if cat == TaskStatus.UNKNOWN and _accounting_complete(
                state, result_dir_vanished=result_root_vanished
            ):
                # Result root vanished + scheduler-terminal exit 0: classify
                # complete on the accounting evidence, with provenance so a
                # consumer can tell this apart from a result-file completion.
                cat = TaskStatus.COMPLETE
                tasks[str(tid)] = {
                    "status": cat,
                    "evidence": "scheduler_accounting",
                    "result_missing": True,
                    **info,
                }
            else:
                tasks[str(tid)] = {"status": cat, **info}
            summary[cat] += 1
        else:
            tasks[str(tid)] = {"status": "unknown"}
            summary["unknown"] += 1

    failed_or_unknown = [tid for tid in range(total_tasks) if tid not in complete_ids]
    all_err = (
        get_err_log_paths(
            job_ids,
            total_tasks,
            scheduler=scheduler,
            log_dir=log_dir,
            scratch_dir=scratch_dir,
            job_name=job_name,
        )
        if job_ids
        else {}
    )
    err_paths = {str(tid): all_err[tid] for tid in failed_or_unknown if tid in all_err}

    from hpc_agent.execution.mapreduce.reduce.metrics import reduce_resource_usage

    report: dict = {
        "result_dir": str(Path(result_dir).resolve()),
        "total_tasks": total_tasks,
        "scheduler": scheduler,
        "timestamp": utcnow_iso(),
        "tasks": tasks,
        "summary": summary,
        "errors": errors,
        "resource_usage": reduce_resource_usage(tasks),
    }
    if err_paths:
        report["err_log_paths"] = err_paths
    # Fresh, free preemption signal: which currently-failed tasks the
    # scheduler bumped (exit 130/143 or state PREEMPTED). Surfaced so the
    # monitor's auto-resume gate (#299) reads it straight off last_status
    # without a second cluster round-trip. Optional — present only when > 0.
    preempted = _preempted_ids_from_tasks(tasks)
    if preempted:
        report["preempted_task_ids"] = preempted
    return report


# ---------------------------------------------------------------------------
# Tasks-driven variants (per-task result directories)
# ---------------------------------------------------------------------------


def check_results_from_tasks(
    tasks_data: dict,
    file_glob: str = "*",
    *,
    min_rows: int = 0,
) -> dict[int, dict]:
    """Mark tasks complete by checking each task's ``result_dir``.

    Consumes a per-task dict — either the synthetic dict produced
    from a per-run sidecar + ``.hpc/tasks.py`` by
    :func:`_build_per_task_dict_from_sidecar`, or any equivalent
    structure with ``tasks.<tid>.result_dir`` fields.  Task IDs in the
    input are 0-based ``HpcTaskId``; the returned dict keeps the same
    0-based space to match :func:`report_status`.

    Completion semantics: a result file is considered complete when it
    exists and is non-zero byte.  CSVs with only a header (e.g. a
    zero-result task) are accepted by default and will not trigger
    auto-resubmit in ``/status``.  Set ``min_rows > 0`` to opt into the
    stricter check that requires at least that many CSV data rows beyond
    the header.
    """
    import csv

    results: dict[int, dict] = {}
    for tid_str, entry in tasks_data.get("tasks", {}).items():
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        result_dir_raw = entry.get("result_dir")
        if not result_dir_raw:
            continue
        rdir = Path(result_dir_raw)
        if not rdir.is_dir():
            continue
        for match in sorted(rdir.glob(file_glob)):
            match_str = str(match)
            if "_wip_" in match_str:
                continue
            try:
                if match.is_file() and match.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            if min_rows > 0 and match_str.endswith(".csv"):
                try:
                    with open(match_str, newline="", encoding="utf-8") as f:
                        reader = csv.reader(f)
                        header = next(reader, None)
                        if header is None:
                            continue
                        row_count = sum(1 for _ in reader)
                        if row_count < min_rows:
                            continue
                    results[tid] = {
                        "status": "complete",
                        "path": match_str,
                        "csv_rows": row_count,
                    }
                    break
                except OSError:
                    continue
            results[tid] = {"status": "complete", "path": match_str}
            break
    return results


def report_status_from_tasks(
    tasks_data: dict,
    job_ids: list[str],
    scheduler: str | None = None,
    *,
    file_glob: str = "*",
    log_dir: str = "",
    scratch_dir: str = "",
    job_name: str = "",
    slurm_cluster: str | None = None,
    sge_user: str | None = None,
    min_rows: int = 0,
) -> dict:
    """Like :func:`report_status` but driven by a per-task dict.

    Uses the per-task ``result_dir`` recorded in each task entry instead of a
    single shared directory.  Consumes the same per-task dict as
    :func:`check_results_from_tasks` — typically synthesized from a
    sidecar + ``.hpc/tasks.py``. ``min_rows`` is forwarded to
    :func:`check_results_from_tasks`; see its docstring for the CSV
    completion semantics.

    Each task's per-task dict includes ``cmd_sha`` pulled from the task
    entry when present; ``null`` otherwise.
    """
    # B5-PR2: per-scheduler job-state query goes through backend.query_jobs.
    from hpc_agent.infra.backends import get_backend_class

    total = int(tasks_data.get("total_tasks", len(tasks_data.get("tasks", {}))))
    task_entries = tasks_data.get("tasks", {}) or {}

    completed = check_results_from_tasks(tasks_data, file_glob=file_glob, min_rows=min_rows)

    if scheduler is None:
        # Pass a representative per-task result_dir so detect_scheduler can
        # consult experiment_meta.json instead of falling back to the
        # ``sacct --version`` shell heuristic — which silently returns "sge"
        # on hosts without sacct on $PATH.
        first_task = next(iter(task_entries.values()), None)
        meta_dir = first_task.get("result_dir") if isinstance(first_task, dict) else None
        scheduler = detect_scheduler(meta_dir)

    errors: list[dict] = []
    if job_ids:
        query_result = get_backend_class(scheduler).query_jobs(
            job_ids, sge_user=sge_user, slurm_cluster=slurm_cluster
        )
        job_info = query_result.get("tasks", {}) or {}
        errors.extend(query_result.get("errors", []) or [])
    else:
        job_info = {}

    def _cmd_sha_for(task_id: int) -> str | None:
        """Look up cmd_sha on the task entry for a 0-based HpcTaskId."""
        entry = task_entries.get(str(task_id))
        if not entry:
            return None
        sha = entry.get("cmd_sha")
        return sha if isinstance(sha, str) else None

    complete_ids = set(completed)
    tasks: dict[str, dict] = {}
    summary = _empty_summary()

    def _result_dir_vanished(task_id: int) -> bool:
        """Per-task vanished signal: the recorded ``result_dir`` no longer exists.

        An empty/unset ``result_dir`` (degraded template) carries no vanish
        evidence — the accounting fallback must not fire on it.
        """
        entry = task_entries.get(str(task_id))
        rd = entry.get("result_dir") if isinstance(entry, dict) else None
        if not isinstance(rd, str) or not rd:
            return False
        return not Path(rd).is_dir()

    # 0-based HpcTaskId throughout (task_entries are keyed str(0..n-1);
    # completed + job_info already speak the domain space).
    for tid in range(total):
        cmd_sha = _cmd_sha_for(tid)
        if tid in complete_ids:
            entry = dict(completed[tid])
            entry["cmd_sha"] = cmd_sha
            tasks[str(tid)] = entry
            summary["complete"] += 1
        elif tid in job_info:
            info = job_info[tid]
            state = info["state"]
            cat = _categorize(state)
            if cat == TaskStatus.UNKNOWN and _accounting_complete(
                state, result_dir_vanished=_result_dir_vanished(tid)
            ):
                # Vanished result dir + scheduler-terminal exit 0: classify
                # complete on the accounting evidence (see _accounting_complete).
                cat = TaskStatus.COMPLETE
                tasks[str(tid)] = {
                    "status": cat,
                    "cmd_sha": cmd_sha,
                    "evidence": "scheduler_accounting",
                    "result_missing": True,
                    **info,
                }
            else:
                tasks[str(tid)] = {"status": cat, "cmd_sha": cmd_sha, **info}
            summary[cat] += 1
        else:
            tasks[str(tid)] = {"status": "unknown", "cmd_sha": cmd_sha}
            summary["unknown"] += 1

    failed_or_unknown = [tid for tid in range(total) if tid not in complete_ids]
    all_err = (
        get_err_log_paths(
            job_ids,
            total,
            scheduler=scheduler,
            log_dir=log_dir,
            scratch_dir=scratch_dir,
            job_name=job_name,
        )
        if job_ids
        else {}
    )
    err_paths = {str(tid): all_err[tid] for tid in failed_or_unknown if tid in all_err}

    from hpc_agent.execution.mapreduce.reduce.metrics import reduce_resource_usage

    report: dict = {
        "total_tasks": total,
        "scheduler": scheduler,
        "timestamp": utcnow_iso(),
        "tasks": tasks,
        "summary": summary,
        "errors": errors,
        "resource_usage": reduce_resource_usage(tasks),
    }
    if err_paths:
        report["err_log_paths"] = err_paths
    # See report_status: surface the fresh scheduler-side preemption signal
    # for the monitor's auto-resume gate (#299). Optional — present only > 0.
    preempted = _preempted_ids_from_tasks(tasks)
    if preempted:
        report["preempted_task_ids"] = preempted
    return report


# ---------------------------------------------------------------------------
# CLI entry point - `python -m hpc_agent.execution.mapreduce.reduce.status`
# ---------------------------------------------------------------------------


# Placeholders the reporter can always supply from the sidecar alone,
# without importing ``tasks.py``. Any OTHER ``{name}`` in
# ``result_dir_template`` must come from ``tasks_module.resolve(i)``.
_SIDECAR_ONLY_PLACEHOLDERS = frozenset({"run_id", "task_id"})


def _template_needs_resolve_kwargs(template: str) -> bool:
    """True when *template* references a placeholder beyond run_id/task_id.

    Parses ``result_dir_template`` for ``{name}`` fields via the stdlib
    formatter. A template like ``results/{run_id}/task_{task_id}`` references
    only sidecar-supplied names, so the per-task ``resolve(i)`` kwargs are
    unused and importing ``tasks.py`` is pure cost (and pure risk — a foreign
    campaign's strategy file may fail to import and would otherwise wedge the
    whole report). Only when a template names some *other* placeholder do we
    actually need the import. An unparseable template is treated as needing
    kwargs (conservative: fall through to the import path, which preserves the
    prior behavior for that edge).
    """
    import string

    try:
        names = {
            field for _literal, field, _spec, _conv in string.Formatter().parse(template) if field
        }
    except (ValueError, TypeError):
        return True
    # An auto-numbered ``{}`` field parses to an empty string (filtered out
    # by ``if field`` above) and a positional ``{0}`` to a digit name; neither
    # is sidecar-supplied, so any leftover non-sidecar name means we need the
    # resolve() kwargs.
    return bool(names - _SIDECAR_ONLY_PLACEHOLDERS)


def _build_per_task_dict_from_sidecar(sidecar: dict, tasks_module) -> dict:
    """Build a per-task dict from sidecar (+ optional ``.hpc/tasks.py``).

    Adapter that lets the existing reporting code
    (``report_status_from_tasks``, ``rollup_by_grid_point``,
    ``rollup_by_wave``) operate unchanged against the new model. Each
    task's ``result_dir`` is computed by formatting the sidecar's
    ``result_dir_template`` against ``task_id`` + ``run_id`` + the
    kwargs returned by ``tasks_module.resolve(task_id)``.

    ``tasks_module`` may be ``None`` — the *degraded* path used when the
    template references no ``resolve()`` kwargs (so the import was skipped)
    or when the import failed for a foreign ``tasks.py``. In that mode each
    task's kwargs are empty: result dirs synthesize from ``run_id``/``task_id``
    only, and ``params`` is ``{}`` (so the grid rollup falls under the ``"_"``
    key rather than crashing the whole report). A template that *does* need
    kwargs but ran in degraded mode formats with an empty context and yields
    an empty ``result_dir`` for the missing fields — surfaced as "incomplete"
    downstream, never an exception.
    """
    n = int(sidecar["task_count"])
    template = sidecar["result_dir_template"]
    run_id = sidecar["run_id"]
    tasks: dict[str, dict] = {}
    for i in range(n):
        kwargs: dict = {}
        if tasks_module is not None:
            resolved = tasks_module.resolve(i)
            if isinstance(resolved, dict):
                kwargs = resolved
        ctx = {"task_id": i, "run_id": run_id, **kwargs}
        try:
            result_dir = template.format(**ctx)
        except (KeyError, IndexError):
            # Surface as empty so downstream "missing result file" logic
            # flags the misconfiguration (or the degraded fallback) without
            # crashing the report.
            result_dir = ""
        tasks[str(i)] = {
            "result_dir": result_dir,
            "params": kwargs,
            "cmd_sha": None,  # cmd_sha lives at the run level in the new model
        }
    return {
        "schema_version": 2,
        "total_tasks": n,
        "tasks": tasks,
        "wave_map": sidecar.get("wave_map", {}),
        "cmd_sha": sidecar.get("cmd_sha"),
        "run_id": run_id,
    }


def _main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Emit a JSON status report for a run.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Run ID — locates the sidecar at .hpc/runs/<run_id>.json.",
    )
    parser.add_argument(
        "--job-ids",
        default="",
        help="Comma-separated scheduler job IDs (optional)",
    )
    parser.add_argument("--job-name", default="", help="Job name for error-log lookup")
    # Free string: the orchestrator supplies an already-validated backend name
    # (the wire ``Scheduler`` type gates it), and the profile engine rejects an
    # unknown family downstream. No closed enum to mirror here (#337).
    parser.add_argument("--scheduler", default=None)
    parser.add_argument("--file-glob", default="*", help="Glob for per-task result files")
    parser.add_argument("--log-dir", default="", help="SLURM log directory")
    parser.add_argument("--scratch-dir", default="", help="SGE scratch log directory")
    parser.add_argument("--slurm-cluster", default=None)
    parser.add_argument("--sge-user", default=None)
    parser.add_argument(
        "--min-rows",
        type=int,
        default=0,
        help="Require CSV results to have at least N data rows beyond the header. "
        "Default 0 accepts header-only CSVs (e.g. zero-result CSVs).",
    )
    args = parser.parse_args()

    from hpc_agent.execution.mapreduce._guard import assert_canonical_import

    # #159: fail loud with a clear, actionable message if a stale/shadowing
    # hpc_agent (e.g. a ~/.local install or a namespace shadow) won the import,
    # instead of emitting wrong results or dying with an opaque error later.
    # Runs AFTER arg-parse so ``--help``/``-h`` short-circuit cleanly via
    # argparse (a help request never needs import-sanity) — which is what lets
    # the deployed namespace-package copy answer ``--help`` self-contained
    # (#349) without the guard rejecting its (intentionally) missing __file__.
    assert_canonical_import()

    def _emit_err(code: str, detail: str, exit_code: int = 2) -> int:
        err_doc = {
            "summary": _empty_summary(),
            "tasks": {},
            "rollup": {},
            "errors": [{"code": code, "detail": detail}],
        }
        json.dump(err_doc, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return exit_code

    # Read .hpc/runs/<run_id>.json + .hpc/tasks.py and synthesize a
    # task-keyed dict the reporting code consumes. Use the canonical
    # hardened reader so wave_map / task_count / result_dir_template are
    # guaranteed to be present.
    from hpc_agent import errors as _errors  # noqa: PLC0415 — lazy
    from hpc_agent.state.runs import read_run_sidecar  # noqa: PLC0415 — lazy

    try:
        sidecar = read_run_sidecar(Path("."), args.run_id)
    except FileNotFoundError:
        sidecar_path = Path(".hpc") / "runs" / f"{args.run_id}.json"
        print(f"run sidecar not found: {sidecar_path}", file=sys.stderr)
        return _emit_err("sidecar_not_found", str(sidecar_path))  # noqa: B904
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, _errors.HpcError) as exc:
        sidecar_path = Path(".hpc") / "runs" / f"{args.run_id}.json"
        return _emit_err("sidecar_parse_error", f"{sidecar_path}: {exc}")

    # Decide whether reporting THIS run actually needs ``tasks.py``. The
    # per-task result dirs come from ``result_dir_template``; only templates
    # that reference a placeholder beyond ``run_id``/``task_id`` need the
    # per-task ``resolve(i)`` kwargs (and thus the import). For the common
    # ``results/{run_id}/task_{task_id}`` shape the import is pure cost — and
    # a foreign campaign's ``tasks.py`` (heavy imports / import-time side
    # effects / missing campaign env vars) would otherwise wedge status for an
    # unrelated run. So import LAZILY, and only when the template needs it.
    template = sidecar.get("result_dir_template", "") or ""
    tasks_module = None
    _degraded_import_detail: str | None = None
    # A genuinely MISSING ``.hpc/tasks.py`` is a malformed run regardless of the
    # template — keep the documented ``tasks_py_not_found`` contract. But only
    # IMPORT it (the wedge cost: a foreign campaign's heavy / env-dependent
    # tasks.py would otherwise block an unrelated run) when ``result_dir_template``
    # actually needs per-task ``resolve()`` kwargs; for the common
    # ``results/{run_id}/task_{task_id}`` shape the import is pure cost.
    tasks_py_path = Path(".hpc") / "tasks.py"
    if not tasks_py_path.is_file():
        return _emit_err("tasks_py_not_found", str(tasks_py_path))
    if _template_needs_resolve_kwargs(template):
        try:
            from hpc_agent import load_tasks_module

            tasks_module = load_tasks_module(tasks_py_path)
        except Exception as exc:
            # DEGRADE rather than fail the whole report: a foreign tasks.py
            # that won't import (present, but heavy / env-dependent) must not
            # block reconciliation of THIS run. Fall back to task_id-only result
            # dirs (tasks_module=None) + a non-fatal note in the errors list.
            print(
                f"tasks.py import failed ({tasks_py_path}: {exc}); "
                "degrading to task_id-only result dirs",
                file=sys.stderr,
            )
            _degraded_import_detail = f"{tasks_py_path}: {exc}"

    try:
        tasks_data = _build_per_task_dict_from_sidecar(sidecar, tasks_module)
    except Exception as exc:
        return _emit_err("synthetic_dict_error", str(exc))

    job_ids = [j for j in args.job_ids.split(",") if j.strip()]

    report = report_status_from_tasks(
        tasks_data,
        job_ids,
        scheduler=args.scheduler,
        file_glob=args.file_glob,
        log_dir=args.log_dir,
        scratch_dir=args.scratch_dir,
        job_name=args.job_name,
        slurm_cluster=args.slurm_cluster,
        sge_user=args.sge_user,
        min_rows=args.min_rows,
    )
    report["rollup"] = rollup_by_grid_point(report, tasks_data)
    report["waves"] = rollup_by_wave(report, tasks_data)

    # Pin all four top-level keys, even if upstream forgot one.
    report.setdefault("summary", _empty_summary())
    report.setdefault("tasks", {})
    report.setdefault("rollup", {})
    report.setdefault("waves", {})
    report.setdefault("errors", [])

    # A degraded import is non-fatal: the report stands (exit 0) but records
    # WHY result dirs were synthesized from task_id only, so the operator can
    # tell a genuinely-empty run apart from one whose tasks.py wouldn't load.
    if _degraded_import_detail is not None:
        report["errors"].append(
            {"code": "tasks_py_import_degraded", "detail": _degraded_import_detail}
        )

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
