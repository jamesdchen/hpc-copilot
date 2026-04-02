"""Job status checking, result validation, and status reporting."""

from __future__ import annotations

__all__ = [
    "check_results",
    "aggregate_counters",
    "report_status",
    "get_err_log_paths",
    "detect_scheduler",
]

import glob
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result checking
# ---------------------------------------------------------------------------

_DEFAULT_RESULT_RE = re.compile(r"(?:results_)?chunk_(\d+)\.csv$", re.IGNORECASE)


def _extract_chunk_id(filename: str, pattern: re.Pattern | None = None) -> int | None:
    """Return the integer chunk id embedded in *filename*, or None."""
    pat = pattern or _DEFAULT_RESULT_RE
    m = pat.search(filename)
    return int(m.group(1)) if m else None


def check_results(
    result_dir: str | Path,
    total_chunks: int,
    file_glob: str = "*chunk_*.csv",
    chunk_pattern: re.Pattern | None = None,
    validate: bool = True,
) -> dict[int, dict]:
    """Scan *result_dir* for completed result files.

    Parameters
    ----------
    result_dir : directory to scan
    total_chunks : expected number of chunks (IDs 1..total_chunks)
    file_glob : glob pattern for result files
    chunk_pattern : regex with group(1) capturing the chunk ID integer.
        Defaults to matching ``chunk_<N>.csv`` or ``results_chunk_<N>.csv``.
    validate : if True and files are CSVs, check for header + >=1 data row
    """
    import csv

    results: dict[int, dict] = {}
    rdir = Path(result_dir).resolve()

    for path_str in glob.glob(str(rdir / file_glob)):
        if "/_wip_" in path_str:
            continue
        chunk_id = _extract_chunk_id(os.path.basename(path_str), chunk_pattern)
        if chunk_id is None or chunk_id < 1 or chunk_id > total_chunks:
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
                results[chunk_id] = {"status": "complete", "csv_rows": row_count}
            except OSError:
                continue
        else:
            results[chunk_id] = {"status": "complete", "path": path_str}

    return results


def aggregate_counters(
    result_dir: str | Path,
    total_chunks: int,
) -> dict:
    """Read and aggregate map-side counters across all chunks.

    Each running task may write a ``_counters_<N>.json`` file via
    :meth:`ChunkContext.update_counters`.  This function collects them
    and produces an aggregated summary.

    Returns
    -------
    dict with keys:
        per_chunk : dict mapping chunk_id (int) to its counter dict
        totals : dict of counter_name -> sum (numeric counters only)
        reporting : int, number of chunks that have written counters
        progress : float or None, estimated 0-1 progress if chunks
            report ``rows_processed`` and ``total_rows``
    """
    rdir = Path(result_dir).resolve()
    counter_pattern = re.compile(r"_counters_(\d+)\.json$")

    per_chunk: dict[int, dict] = {}

    for path_str in glob.glob(str(rdir / "_counters_*.json")):
        m = counter_pattern.search(path_str)
        if m is None:
            continue
        chunk_id = int(m.group(1))
        try:
            with open(path_str) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        per_chunk[chunk_id] = data

    # Aggregate numeric values across all chunks.
    totals: dict[str, int | float] = {}
    for counters in per_chunk.values():
        for key, value in counters.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value

    # Compute progress estimate.
    progress: float | None = None
    if "rows_processed" in totals and "total_rows" in totals:
        total_rows = totals["total_rows"]
        if total_rows > 0:
            progress = totals["rows_processed"] / total_rows

    return {
        "per_chunk": per_chunk,
        "totals": totals,
        "reporting": len(per_chunk),
        "progress": progress,
    }


# ---------------------------------------------------------------------------
# Scheduler detection
# ---------------------------------------------------------------------------


def detect_scheduler(result_dir: str | Path | None = None) -> str:
    """Auto-detect scheduler type.

    Checks (in order):
    1. experiment_meta.json in result_dir (if provided)
    2. Probe for sacct (SLURM)
    3. Fall back to "sge"
    """
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
    total_chunks: int,
    scheduler: str = "slurm",
    log_dir: str = "",
    job_name: str = "",
    scratch_dir: str = "",
) -> dict[int, str]:
    """Find the most recent error log path on disk for each chunk.

    Parameters
    ----------
    log_dir : directory for SLURM logs (e.g. /path/to/logs)
    scratch_dir : directory for SGE logs (e.g. $SCRATCH)
    """
    paths: dict[int, str] = {}
    for tid in range(1, total_chunks + 1):
        for job_id in reversed(job_ids):
            if scheduler == "sge":
                p = os.path.join(scratch_dir, f"{job_name}.o{job_id}.{tid}")
            else:
                # Canonical: {job_name}_{job_id}_{tid}.err
                p = os.path.join(log_dir, f"{job_name}_{job_id}_{tid}.err")
                if not os.path.isfile(p):
                    # Fallback: glob for any prefix matching this job+task
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
    total_chunks: int,
    scheduler: str | None = None,
    *,
    file_glob: str = "*chunk_*.csv",
    chunk_pattern: re.Pattern | None = None,
    log_dir: str = "",
    scratch_dir: str = "",
    job_name: str = "",
    slurm_cluster: str | None = None,
    sge_user: str | None = None,
    include_counters: bool = False,
) -> dict:
    """Assemble a full JSON status report.

    Parameters
    ----------
    result_dir : directory containing result files
    job_ids : scheduler job IDs to query
    total_chunks : expected number of chunks
    scheduler : "slurm" or "sge" (auto-detected if None)
    file_glob, chunk_pattern : forwarded to check_results
    log_dir, scratch_dir, job_name : forwarded to get_err_log_paths
    slurm_cluster : --clusters flag for sacct
    sge_user : user for qstat -u
    """
    from hpc.backends.query import query_sacct, query_sge

    csv_results = check_results(
        result_dir, total_chunks, file_glob=file_glob, chunk_pattern=chunk_pattern
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
    chunks: dict[str, dict] = {}
    summary = {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}

    for tid in range(1, total_chunks + 1):
        if tid in complete_ids:
            chunks[str(tid)] = csv_results[tid]
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
            chunks[str(tid)] = {"status": cat, **info}
            summary[cat] += 1
        else:
            chunks[str(tid)] = {"status": "unknown"}
            summary["unknown"] += 1

    # Error log paths for non-complete chunks
    failed_or_unknown = [tid for tid in range(1, total_chunks + 1) if tid not in complete_ids]
    all_err = (
        get_err_log_paths(
            job_ids,
            total_chunks,
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
        "total_chunks": total_chunks,
        "scheduler": scheduler,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "chunks": chunks,
        "summary": summary,
    }
    if err_paths:
        report["err_log_paths"] = err_paths
    if query_error:
        report["query_error"] = query_error
    if include_counters:
        report["counters"] = aggregate_counters(result_dir, total_chunks)
    return report
