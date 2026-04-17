"""Job status checking, result validation, and status reporting.

This module drives the LLM-orchestrator's ``/monitor`` loop.  The CLI entry
point (``python -m hpc_mapreduce.reduce.status --manifest ...``) emits JSON
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

``cmd_sha`` is passed through from the manifest (schema v2); absent on v1
manifests -> serialized as ``null`` for each task.  Additional top-level
keys (``total_tasks``, ``scheduler``, ``timestamp``, ``result_dir``,
``err_log_paths``, ``resource_usage``) may appear but are informational
only; the four keys above are the parse contract.

``resource_usage`` is additive and shaped like::

    {"cpu_hours": float, "gpu_hours": float,
     "elapsed_hours": float, "tasks_counted": int}

Values are summed across all tasks in the status report (not just
completed ones) using whatever the scheduler has reported so far.
"""

from __future__ import annotations

__all__ = [
    "check_results",
    "check_results_from_manifest",
    "report_status",
    "report_status_from_manifest",
    "rollup_by_grid_point",
    "get_err_log_paths",
    "detect_scheduler",
]

import glob
import json
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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
    legitimately-empty outputs (e.g. zero-trade backtest periods) still count
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
    """Auto-detect scheduler type."""
    if result_dir is not None:
        meta_path = Path(result_dir) / "experiment_meta.json"
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
    """Find the most recent error log path on disk for each task."""
    paths: dict[int, str] = {}
    for tid in range(1, total_tasks + 1):
        for job_id in reversed(job_ids):
            if scheduler == "sge":
                p = os.path.join(scratch_dir, f"{job_name}.o{job_id}.{tid}")
            else:
                p = os.path.join(log_dir, f"{job_name}_{job_id}_{tid}.err")
                if not os.path.isfile(p):
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
    """Return the canonical zeroed summary dict (5 int keys, always present)."""
    return {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}


def _categorize(state: str) -> str:
    """Map a scheduler state string to a summary bucket name."""
    if state in _ACTIVE_STATES:
        return "running"
    if state in _PENDING_STATES:
        return "pending"
    if state in _FAILED_STATES or state.startswith("CANCELLED"):
        return "failed"
    return "unknown"


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
    from hpc_mapreduce.infra.backends.query import query_sacct, query_sge

    csv_results = check_results(result_dir, total_tasks, file_glob=file_glob, min_rows=min_rows)

    if scheduler is None:
        scheduler = detect_scheduler(result_dir)

    errors: list[dict] = []
    if job_ids:
        if scheduler == "sge":
            query_result = query_sge(job_ids, user=sge_user)
        else:
            query_result = query_sacct(job_ids, cluster=slurm_cluster)
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

    from hpc_mapreduce.reduce.metrics import reduce_resource_usage

    report: dict = {
        "result_dir": str(Path(result_dir).resolve()),
        "total_tasks": total_tasks,
        "scheduler": scheduler,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks": tasks,
        "summary": summary,
        "errors": errors,
        "resource_usage": reduce_resource_usage(tasks),
    }
    if err_paths:
        report["err_log_paths"] = err_paths
    return report


# ---------------------------------------------------------------------------
# Manifest-driven variants (per-task result directories)
# ---------------------------------------------------------------------------


def _grid_point_key(params: dict) -> str:
    """Stable grid-point identifier from a params dict."""
    if not params:
        return "_"
    return "_".join(f"{k}={params[k]}" for k in sorted(params))


def check_results_from_manifest(
    manifest: dict,
    file_glob: str = "*",
    *,
    min_rows: int = 0,
) -> dict[int, dict]:
    """Mark tasks complete by checking their per-task ``result_dir`` from a dispatch manifest.

    Manifest task IDs are 0-based; returned dict uses 1-based task IDs to match
    :func:`report_status`.

    Completion semantics: a result file is considered complete when it exists and
    is non-zero byte.  CSVs with only a header (e.g. a backtest period with zero
    trades) are therefore accepted by default and will not trigger auto-resubmit
    in ``/monitor``.  Set ``min_rows > 0`` to opt into the stricter check that
    requires at least that many CSV data rows beyond the header.
    """
    import csv

    results: dict[int, dict] = {}
    for tid_str, entry in manifest.get("tasks", {}).items():
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


def report_status_from_manifest(
    manifest: dict,
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
    """Like :func:`report_status` but driven by a dispatch manifest.

    Uses the per-task ``result_dir`` recorded in each manifest entry instead of a single
    shared directory.  ``min_rows`` is forwarded to
    :func:`check_results_from_manifest`; see its docstring for the CSV
    completion semantics.

    Each task's per-task dict includes ``cmd_sha`` pulled from the manifest
    entry when present (manifest schema v2+); ``null`` otherwise (v1 back-compat).
    """
    from hpc_mapreduce.infra.backends.query import query_sacct, query_sge

    total = int(manifest.get("total_tasks", len(manifest.get("tasks", {}))))
    manifest_tasks = manifest.get("tasks", {}) or {}

    completed = check_results_from_manifest(manifest, file_glob=file_glob, min_rows=min_rows)

    if scheduler is None:
        scheduler = detect_scheduler()

    errors: list[dict] = []
    if job_ids:
        if scheduler == "sge":
            query_result = query_sge(job_ids, user=sge_user)
        else:
            query_result = query_sacct(job_ids, cluster=slurm_cluster)
        job_info = query_result.get("tasks", {}) or {}
        errors.extend(query_result.get("errors", []) or [])
    else:
        job_info = {}

    def _cmd_sha_for(one_based_tid: int) -> str | None:
        """Look up cmd_sha on the manifest entry for a 1-based task id."""
        entry = manifest_tasks.get(str(one_based_tid - 1))
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

    from hpc_mapreduce.reduce.metrics import reduce_resource_usage

    report: dict = {
        "total_tasks": total,
        "scheduler": scheduler,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks": tasks,
        "summary": summary,
        "errors": errors,
        "resource_usage": reduce_resource_usage(tasks),
    }
    if err_paths:
        report["err_log_paths"] = err_paths
    return report


def rollup_by_grid_point(report: dict, manifest: dict) -> dict[str, dict]:
    """Group per-task statuses in *report* by grid point (from manifest ``params``).

    Manifest task IDs are 0-based strings; report task IDs are 1-based strings.
    Returned dict maps grid-point key -> ``{complete, running, pending, failed, unknown, total}``.
    """
    rollup: dict[str, dict] = {}
    manifest_tasks = manifest.get("tasks", {})
    for tid_str, task_info in report.get("tasks", {}).items():
        try:
            manifest_key = str(int(tid_str) - 1)
        except (TypeError, ValueError):
            continue
        entry = manifest_tasks.get(manifest_key)
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


# ---------------------------------------------------------------------------
# CLI entry point - `python -m hpc_mapreduce.reduce.status`
# ---------------------------------------------------------------------------


def _main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Emit a JSON status report for a dispatch manifest.",
    )
    parser.add_argument("--manifest", required=True, help="Path to _hpc_dispatch.json")
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
        "Default 0 accepts header-only CSVs (e.g. zero-trade backtest periods).",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        # Even on error, emit the pinned 4-key shape so the LLM orchestrator
        # can parse stdout unconditionally.
        err_doc = {
            "summary": _empty_summary(),
            "tasks": {},
            "rollup": {},
            "errors": [
                {"code": "manifest_not_found", "detail": str(manifest_path)},
            ],
        }
        json.dump(err_doc, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        err_doc = {
            "summary": _empty_summary(),
            "tasks": {},
            "rollup": {},
            "errors": [
                {"code": "manifest_parse_error", "detail": f"{manifest_path}: {exc}"},
            ],
        }
        json.dump(err_doc, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 2

    job_ids = [j for j in args.job_ids.split(",") if j.strip()]

    report = report_status_from_manifest(
        manifest,
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
    report["rollup"] = rollup_by_grid_point(report, manifest)

    # Pin all four top-level keys, even if upstream forgot one.
    report.setdefault("summary", _empty_summary())
    report.setdefault("tasks", {})
    report.setdefault("rollup", {})
    report.setdefault("errors", [])

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
