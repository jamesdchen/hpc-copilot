"""Job status checking, result validation, and status reporting."""

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
) -> dict[int, dict]:
    """Scan *result_dir* for completed result files.

    Looks for result files matching *file_glob* in per-task subdirectories
    or directly in *result_dir*.  Returns a dict mapping task id to status info.
    """
    import csv

    results: dict[int, dict] = {}
    rdir = Path(result_dir).resolve()

    # Strategy 1: check per-task subdirectories (task_1/, task_2/, ...)
    for tid in range(1, total_tasks + 1):
        task_dir = rdir / f"task_{tid}"
        if task_dir.is_dir():
            for path_str in glob.glob(str(task_dir / file_glob)):
                if "/_wip_" in path_str:
                    continue
                if validate and path_str.endswith(".csv"):
                    try:
                        with open(path_str, newline="") as f:
                            reader = csv.reader(f)
                            header = next(reader, None)
                            if header is None:
                                continue
                            row_count = sum(1 for _ in reader)
                            if row_count < 1:
                                continue
                        results[tid] = {"status": "complete", "csv_rows": row_count}
                    except OSError:
                        continue
                else:
                    results[tid] = {"status": "complete", "path": path_str}
                break  # one match per task is enough

    # Strategy 2: fall back to flat directory scan if no task subdirs found
    if not results:
        for path_str in glob.glob(str(rdir / file_glob)):
            if "/_wip_" in path_str:
                continue
            if validate and path_str.endswith(".csv"):
                try:
                    with open(path_str, newline="") as f:
                        reader = csv.reader(f)
                        header = next(reader, None)
                        if header is None:
                            continue
                        row_count = sum(1 for _ in reader)
                        if row_count < 1:
                            continue
                    # Use index as task id for flat results
                    tid = len(results) + 1
                    if tid > total_tasks:
                        break
                    results[tid] = {"status": "complete", "csv_rows": row_count}
                except OSError:
                    continue
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
) -> dict:
    """Assemble a full JSON status report."""
    from hpc_mapreduce.infra.backends.query import query_sacct, query_sge

    csv_results = check_results(
        result_dir, total_tasks, file_glob=file_glob
    )

    if scheduler is None:
        scheduler = detect_scheduler(result_dir)

    if job_ids:
        if scheduler == "sge":
            job_info = query_sge(job_ids, user=sge_user)
        else:
            job_info = query_sacct(job_ids, cluster=slurm_cluster)
    else:
        job_info = {}
    query_error = job_info.pop("error", None)

    complete_ids = set(csv_results)
    tasks: dict[str, dict] = {}
    summary = {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}

    for tid in range(1, total_tasks + 1):
        if tid in complete_ids:
            tasks[str(tid)] = csv_results[tid]
            summary["complete"] += 1
        elif tid in job_info:
            info = job_info[tid]
            state = info["state"]
            if state in _ACTIVE_STATES:
                cat = "running"
            elif state in _PENDING_STATES:
                cat = "pending"
            elif state in _FAILED_STATES or state.startswith("CANCELLED"):
                cat = "failed"
            else:
                cat = "unknown"
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

    report: dict = {
        "result_dir": str(Path(result_dir).resolve()),
        "total_tasks": total_tasks,
        "scheduler": scheduler,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks": tasks,
        "summary": summary,
    }
    if err_paths:
        report["err_log_paths"] = err_paths
    if query_error:
        report["query_error"] = query_error
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
) -> dict[int, dict]:
    """Mark tasks complete by checking their per-task ``result_dir`` from a dispatch manifest.

    Manifest task IDs are 0-based; returned dict uses 1-based task IDs to match
    :func:`report_status`.
    """
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
            if "_wip_" in str(match):
                continue
            results[tid] = {"status": "complete", "path": str(match)}
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
) -> dict:
    """Like :func:`report_status` but driven by a dispatch manifest.

    Uses the per-task ``result_dir`` recorded in each manifest entry instead of a single
    shared directory.
    """
    from hpc_mapreduce.infra.backends.query import query_sacct, query_sge

    total = int(manifest.get("total_tasks", len(manifest.get("tasks", {}))))

    completed = check_results_from_manifest(manifest, file_glob=file_glob)

    if scheduler is None:
        scheduler = detect_scheduler()

    if job_ids:
        if scheduler == "sge":
            job_info = query_sge(job_ids, user=sge_user)
        else:
            job_info = query_sacct(job_ids, cluster=slurm_cluster)
    else:
        job_info = {}
    query_error = job_info.pop("error", None)

    complete_ids = set(completed)
    tasks: dict[str, dict] = {}
    summary = {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}

    for tid in range(1, total + 1):
        if tid in complete_ids:
            tasks[str(tid)] = completed[tid]
            summary["complete"] += 1
        elif tid in job_info:
            info = job_info[tid]
            state = info["state"]
            if state in _ACTIVE_STATES:
                cat = "running"
            elif state in _PENDING_STATES:
                cat = "pending"
            elif state in _FAILED_STATES or state.startswith("CANCELLED"):
                cat = "failed"
            else:
                cat = "unknown"
            tasks[str(tid)] = {"status": cat, **info}
            summary[cat] += 1
        else:
            tasks[str(tid)] = {"status": "unknown"}
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

    report: dict = {
        "total_tasks": total,
        "scheduler": scheduler,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks": tasks,
        "summary": summary,
    }
    if err_paths:
        report["err_log_paths"] = err_paths
    if query_error:
        report["query_error"] = query_error
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
# CLI entry point — `python -m hpc_mapreduce.reduce.status`
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
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text())
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
    )
    report["rollup"] = rollup_by_grid_point(report, manifest)

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
