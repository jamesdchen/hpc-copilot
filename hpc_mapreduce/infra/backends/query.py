"""Scheduler-specific job status queries (SGE and SLURM)."""

from __future__ import annotations

__all__ = [
    "query_sacct",
    "query_sge",
]

import os
import re
import subprocess

# ---------------------------------------------------------------------------
# SLURM
# ---------------------------------------------------------------------------


def query_sacct(job_ids: list[str], cluster: str | None = None) -> dict:
    """Query SLURM sacct for array task states.

    Returns {task_id: {state, exit_code, job_id}} or {"error": ...}.
    """
    task_info: dict[int, dict] = {}

    for job_id in job_ids:
        cmd = ["sacct", "-j", job_id, "--format=JobID,State,ExitCode", "--noheader", "--parsable2"]
        if cluster:
            cmd.insert(1, f"--clusters={cluster}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {"error": "sacct_unavailable"}

        if result.returncode != 0 or not result.stdout.strip():
            if not task_info:
                return {"error": "sacct_unavailable"}
            continue

        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            job_field, state, exit_code = parts[0], parts[1], parts[2]
            if "_" not in job_field:
                continue
            try:
                tid = int(job_field.split("_")[1])
            except (IndexError, ValueError):
                continue
            task_info[tid] = {"state": state, "exit_code": exit_code, "job_id": job_id}

    if not task_info:
        return {"error": "sacct_unavailable"}
    return task_info


# ---------------------------------------------------------------------------
# SGE
# ---------------------------------------------------------------------------

# SGE state code -> normalized state
_SGE_STATE_MAP: dict[str, str] = {
    "r": "RUNNING",
    "t": "RUNNING",
    "Rr": "RUNNING",
    "Rt": "RUNNING",
    "qw": "PENDING",
    "hqw": "PENDING",
    "Eqw": "FAILED",
    "Ehqw": "FAILED",
    "dr": "CANCELLED",
    "dt": "CANCELLED",
    "dRr": "CANCELLED",
    "dRt": "CANCELLED",
    "ds": "CANCELLED",
    "dS": "CANCELLED",
    "dT": "CANCELLED",
}


def _expand_task_range(spec: str) -> list[int]:
    """Expand an SGE task range like '3-10:1' or '5' into a list of ints."""
    spec = spec.strip()
    if not spec or spec == "undefined":
        return []
    m = re.match(r"(\d+)(?:-(\d+)(?::(\d+))?)?", spec)
    if not m:
        return []
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    step = int(m.group(3)) if m.group(3) else 1
    return list(range(start, end + 1, step))


def _process_qacct_block(
    block: dict[str, str],
    job_id: str,
    task_info: dict[int, dict],
) -> None:
    """Extract task status from a single qacct block."""
    tid_str = block.get("taskid", "")
    if not tid_str or tid_str == "undefined":
        return
    try:
        tid = int(tid_str)
    except ValueError:
        return
    if tid in task_info:
        return  # qstat data takes precedence

    exit_status = block.get("exit_status", "0")
    failed = block.get("failed", "0")
    try:
        exit_int = int(exit_status)
        failed_int = int(failed.split()[0]) if failed else 0
    except ValueError:
        exit_int, failed_int = -1, -1

    if exit_int == 0 and failed_int == 0:
        state = "COMPLETED"
    elif failed_int == 100:
        state = "TIMEOUT"
    elif failed_int != 0:
        state = "NODE_FAIL"
    else:
        state = "FAILED"

    task_info[tid] = {"state": state, "exit_code": exit_status, "job_id": job_id}


def query_sge(job_ids: list[str], user: str | None = None) -> dict:
    """Query SGE via qstat + qacct for array task states.

    Returns {task_id: {state, exit_code, job_id}} or {"error": ...}.
    """
    task_info: dict[int, dict] = {}
    sge_user = user or os.environ.get("USER", os.environ.get("USERNAME", ""))

    # Phase 1: qstat for running/pending tasks
    try:
        result = subprocess.run(
            ["qstat", "-u", sge_user],
            capture_output=True,
            text=True,
            timeout=30,
        )
        qstat_out = result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        qstat_out = ""

    job_id_set = set(job_ids)
    for line in qstat_out.strip().splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        jid = cols[0].strip()
        if jid not in job_id_set:
            continue
        state_code = cols[4].strip()
        normalized = _SGE_STATE_MAP.get(state_code, "UNKNOWN")
        task_spec = cols[-1].strip() if len(cols) >= 9 else ""
        for tid in _expand_task_range(task_spec):
            task_info[tid] = {"state": normalized, "exit_code": None, "job_id": jid}

    # Phase 2: qacct for finished tasks
    for job_id in job_ids:
        try:
            result = subprocess.run(
                ["qacct", "-j", job_id],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if result.returncode != 0:
            continue

        current: dict[str, str] = {}
        for raw_line in result.stdout.splitlines():
            if raw_line.startswith("====="):
                if current:
                    _process_qacct_block(current, job_id, task_info)
                    current = {}
                continue
            parts = raw_line.split(None, 1)
            if len(parts) == 2:
                current[parts[0]] = parts[1].strip()
        if current:
            _process_qacct_block(current, job_id, task_info)

    if not task_info:
        return {"error": "sge_unavailable"}
    return task_info
