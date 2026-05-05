"""Job status checking, result validation, and status reporting.

This module drives the LLM-orchestrator's ``/status`` loop.  The CLI entry
point (``python -m claude_hpc.mapreduce.reduce.status --run-id <id>``) emits JSON
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
    "check_results",
    "check_results_from_tasks",
    "report_status",
    "report_status_from_tasks",
    "rollup_by_grid_point",
    "rollup_by_wave",
    "get_err_log_paths",
    "detect_scheduler",
]

import glob
import json
import os
import subprocess
from pathlib import Path

from claude_hpc._internal._time import utcnow_iso
from claude_hpc._internal.lifecycle import TaskStatus

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
    or directly in *result_dir*.  Returns a dict mapping task id to status info.

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
            with open(path_str, newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    return None
                row_count = sum(1 for _ in reader)
                if row_count < min_rows:
                    return None
            return {"status": "complete", "csv_rows": row_count}
        except OSError:
            return None

    # Strategy 1: check per-task subdirectories (task_1/, task_2/, ...)
    for tid in range(1, total_tasks + 1):
        task_dir = rdir / f"task_{tid}"
        if task_dir.is_dir():
            for path_str in glob.glob(str(task_dir / file_glob)):
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

    # Strategy 2: fall back to flat directory scan if no task subdirs found
    if not results:
        for path_str in glob.glob(str(rdir / file_glob)):
            if "/_wip_" in path_str:
                continue
            if validate and path_str.endswith(".csv"):
                status = _accept_csv(path_str)
                if status is None:
                    continue
                tid = len(results) + 1
                if tid > total_tasks:
                    break
                results[tid] = status
            else:
                tid = len(results) + 1
                if tid > total_tasks:
                    break
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
                    meta = json.loads(meta_path.read_text())
                    backend = meta.get("backend", "")
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
        result = subprocess.run(["sacct", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return "slurm"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "sge"


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

    B5-PR2: per-scheduler base path goes through
    :meth:`HPCBackend.err_log_disk_path`. The SLURM fallback glob (which
    catches submission scripts that override ``--error`` to a non-canonical
    name) stays here because it's an on-disk recovery pattern, not a
    scheduler shape question.
    """
    from claude_hpc.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    paths: dict[int, str] = {}
    for tid in range(1, total_tasks + 1):
        for job_id in reversed(job_ids):
            p = backend_cls.err_log_disk_path(log_dir, scratch_dir, job_name, job_id, tid)
            if scheduler != "sge" and not os.path.isfile(p):
                matches = glob.glob(os.path.join(log_dir, f"*{job_id}_{tid}.err"))
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


def _empty_summary() -> dict[str, int]:
    """Return the canonical zeroed summary dict (5 int keys, always present).

    Keys derived from :class:`claude_hpc._internal.lifecycle.TaskStatus` (B2).
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
    from claude_hpc.infra.backends import get_backend_class

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

    for tid in range(1, total_tasks + 1):
        if tid in complete_ids:
            tasks[str(tid)] = csv_results[tid]
            summary["complete"] += 1
        elif tid in job_info:
            info = job_info[tid]
            state = info["state"]
            cat = _categorize(state)
            tasks[str(tid)] = {"status": cat, **info}
            summary[cat] += 1
        else:
            tasks[str(tid)] = {"status": "unknown"}
            summary["unknown"] += 1

    failed_or_unknown = [tid for tid in range(1, total_tasks + 1) if tid not in complete_ids]
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

    from claude_hpc.mapreduce.reduce.metrics import reduce_resource_usage

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
    return report


# ---------------------------------------------------------------------------
# Tasks-driven variants (per-task result directories)
# ---------------------------------------------------------------------------


def _grid_point_key(params: dict) -> str:
    """Stable grid-point identifier from a params dict."""
    if not params:
        return "_"
    return "_".join(f"{k}={params[k]}" for k in sorted(params))


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
    input are 0-based; returned dict uses 1-based task IDs to match
    :func:`report_status`.

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
            tid = int(tid_str) + 1
        except (TypeError, ValueError):
            continue
        result_dir_raw = entry.get("result_dir")
        if not result_dir_raw:
            continue
        rdir = Path(result_dir_raw)
        if not rdir.is_dir():
            continue
        for match in rdir.glob(file_glob):
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
                    with open(match_str, newline="") as f:
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
    from claude_hpc.infra.backends import get_backend_class

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

    def _cmd_sha_for(one_based_tid: int) -> str | None:
        """Look up cmd_sha on the task entry for a 1-based task id."""
        entry = task_entries.get(str(one_based_tid - 1))
        if not entry:
            return None
        sha = entry.get("cmd_sha")
        return sha if isinstance(sha, str) else None

    complete_ids = set(completed)
    tasks: dict[str, dict] = {}
    summary = _empty_summary()

    for tid in range(1, total + 1):
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
            tasks[str(tid)] = {"status": cat, "cmd_sha": cmd_sha, **info}
            summary[cat] += 1
        else:
            tasks[str(tid)] = {"status": "unknown", "cmd_sha": cmd_sha}
            summary["unknown"] += 1

    failed_or_unknown = [tid for tid in range(1, total + 1) if tid not in complete_ids]
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

    from claude_hpc.mapreduce.reduce.metrics import reduce_resource_usage

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
    return report


def rollup_by_grid_point(report: dict, tasks_data: dict) -> dict[str, dict]:
    """Group per-task statuses in *report* by grid point (from task ``params``).

    Per-task dict task IDs are 0-based strings; report task IDs are 1-based strings.
    Returned dict maps grid-point key -> ``{complete, running, pending, failed, unknown, total}``.
    """
    rollup: dict[str, dict] = {}
    task_entries = tasks_data.get("tasks", {})
    for tid_str, task_info in report.get("tasks", {}).items():
        try:
            entry_key = str(int(tid_str) - 1)
        except (TypeError, ValueError):
            continue
        entry = task_entries.get(entry_key)
        if entry is None:
            continue
        gp = _grid_point_key(entry.get("params") or {})
        bucket = rollup.setdefault(
            gp,
            {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0, "total": 0},
        )
        bucket["total"] += 1
        status = task_info.get("status", "unknown")
        if status in bucket:
            bucket[status] += 1
        else:
            bucket["unknown"] += 1
    return rollup


def rollup_by_wave(report: dict, tasks_data: dict) -> dict[str, dict]:
    """Group per-task statuses by wave (from task ``wave_map``).

    Returns ``{wave: {complete, running, pending, failed, unknown, total}}``.
    Empty when the per-task dict has no ``wave_map`` (un-batched submissions).

    Wave map keys are stored as 0-based task ids; the
    status report keys tasks 1-based to match scheduler array indexing,
    so we shift on lookup.
    """
    wave_map = tasks_data.get("wave_map") or {}
    if not wave_map:
        return {}
    report_tasks = report.get("tasks", {}) or {}
    rollup: dict[str, dict] = {}
    for wave_key, members in wave_map.items():
        bucket = {
            "complete": 0,
            "running": 0,
            "pending": 0,
            "failed": 0,
            "unknown": 0,
            "total": 0,
        }
        for tid in members or []:
            bucket["total"] += 1
            # Per-task dict stores 0-based; report keys 1-based.
            try:
                report_key = str(int(tid) + 1)
            except (TypeError, ValueError):
                report_key = str(tid)
            task_info = report_tasks.get(report_key) or {}
            status = task_info.get("status", "unknown")
            if status in bucket:
                bucket[status] += 1
            else:
                bucket["unknown"] += 1
        rollup[str(wave_key)] = bucket
    return rollup


# ---------------------------------------------------------------------------
# CLI entry point - `python -m claude_hpc.mapreduce.reduce.status`
# ---------------------------------------------------------------------------


def _build_per_task_dict_from_sidecar(sidecar: dict, tasks_module) -> dict:
    """Build a per-task dict from sidecar + ``.hpc/tasks.py``.

    Adapter that lets the existing reporting code
    (``report_status_from_tasks``, ``rollup_by_grid_point``,
    ``rollup_by_wave``) operate unchanged against the new model. Each
    task's ``result_dir`` is computed by formatting the sidecar's
    ``result_dir_template`` against ``task_id`` + ``run_id`` + the
    kwargs returned by ``tasks_module.resolve(task_id)``.
    """
    n = int(sidecar["task_count"])
    template = sidecar["result_dir_template"]
    run_id = sidecar["run_id"]
    tasks: dict[str, dict] = {}
    for i in range(n):
        kwargs = tasks_module.resolve(i)
        if not isinstance(kwargs, dict):
            kwargs = {}
        ctx = {"task_id": i, "run_id": run_id, **kwargs}
        try:
            result_dir = template.format(**ctx)
        except KeyError:
            # Surface as empty so downstream "missing result file" logic
            # flags the misconfiguration without crashing the report.
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
    parser.add_argument("--scheduler", default=None, choices=[None, "sge", "slurm"])
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
    from claude_hpc.state.runs import read_run_sidecar  # noqa: PLC0415 — lazy

    try:
        sidecar = read_run_sidecar(Path("."), args.run_id)
    except FileNotFoundError:
        sidecar_path = Path(".hpc") / "runs" / f"{args.run_id}.json"
        print(f"run sidecar not found: {sidecar_path}", file=sys.stderr)
        return _emit_err("sidecar_not_found", str(sidecar_path))  # noqa: B904
    except (OSError, json.JSONDecodeError) as exc:
        sidecar_path = Path(".hpc") / "runs" / f"{args.run_id}.json"
        return _emit_err("sidecar_parse_error", f"{sidecar_path}: {exc}")

    tasks_py_path = Path(".hpc") / "tasks.py"
    if not tasks_py_path.is_file():
        return _emit_err("tasks_py_not_found", str(tasks_py_path))
    try:
        from claude_hpc import load_tasks_module

        tasks_module = load_tasks_module(tasks_py_path)
    except Exception as exc:
        return _emit_err("tasks_py_import_error", f"{tasks_py_path}: {exc}")

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

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
