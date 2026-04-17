"""Scheduler-specific job status queries (SGE and SLURM).

Batched-poll variant: each call to :func:`query_sacct` / :func:`query_sge`
spawns **at most one** subprocess per scheduler tool rather than one per
job ID.  This drastically reduces the number of SSH round-trips the
/monitor loop incurs when many waves x batches are in flight.

Return shape (uniform across happy / error paths)::

    {
        "tasks": {task_id: {"state": str, "exit_code": str | None, "job_id": str}, ...},
        "errors": [{"code": str, "detail": str}, ...],
    }

- Happy path: ``errors`` is ``[]``.
- Tool missing / timeout: ``tasks`` is ``{}`` and ``errors`` has one entry.
- Partial failures: both populated.
"""

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

    Issues a single ``sacct`` call with a comma-joined ``-j`` list and maps
    each resulting row back to its originating job ID.

    Returns ``{"tasks": {task_id: {state, exit_code, job_id}, ...},
    "errors": [{code, detail}, ...]}``.
    """
    if not job_ids:
        return {"tasks": {}, "errors": []}

    task_info: dict[int, dict] = {}
    errors: list[dict] = []
    job_id_set = {str(j) for j in job_ids}
    joined = ",".join(str(j) for j in job_ids)

    cmd = ["sacct", "-j", joined, "--format=JobID,State,ExitCode", "--noheader", "--parsable2"]
    if cluster:
        cmd.insert(1, f"--clusters={cluster}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        return {
            "tasks": {},
            "errors": [{"code": "sacct_unavailable", "detail": f"timeout: {exc}"}],
        }
    except FileNotFoundError as exc:
        return {
            "tasks": {},
            "errors": [{"code": "sacct_unavailable", "detail": f"binary not found: {exc}"}],
        }

    if result.returncode != 0:
        return {
            "tasks": {},
            "errors": [
                {
                    "code": "sacct_unavailable",
                    "detail": f"sacct exit {result.returncode}: {result.stderr.strip()}",
                }
            ],
        }

    if not result.stdout.strip():
        return {
            "tasks": {},
            "errors": [{"code": "sacct_unavailable", "detail": "empty sacct output"}],
        }

    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            errors.append({"code": "malformed_row", "detail": f"expected >=3 fields: {line!r}"})
            continue
        job_field, state, exit_code = parts[0], parts[1], parts[2]
        if "_" not in job_field:
            continue
        # job_field looks like "12345_7" or "12345_7.batch"; strip trailing step.
        base_job, _, tail = job_field.partition("_")
        if base_job not in job_id_set:
            # sacct may return unrelated rows (shouldn't, but be defensive).
            continue
        # tail may be "7", "7.batch", "7.extern"; take the leading integer.
        tail = tail.split(".", 1)[0]
        try:
            tid = int(tail)
        except ValueError:
            errors.append({"code": "malformed_row", "detail": f"non-integer task id: {line!r}"})
            continue
        # First occurrence wins - main record comes before .batch/.extern steps.
        if tid in task_info:
            continue
        task_info[tid] = {"state": state, "exit_code": exit_code, "job_id": base_job}

    return {"tasks": task_info, "errors": errors}


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
    errors: list[dict],
) -> None:
    """Extract task status from a single qacct block."""
    tid_str = block.get("taskid", "")
    if not tid_str or tid_str == "undefined":
        return
    try:
        tid = int(tid_str)
    except ValueError:
        errors.append({"code": "malformed_row", "detail": f"qacct non-integer taskid: {tid_str!r}"})
        return
    if tid in task_info:
        return  # qstat data takes precedence

    exit_status = block.get("exit_status", "0")
    failed = block.get("failed", "0")
    try:
        exit_int = int(exit_status)
        failed_int = int(failed.split()[0]) if failed else 0
    except ValueError:
        errors.append(
            {
                "code": "malformed_row",
                "detail": f"qacct non-integer exit/failed for job {job_id}",
            }
        )
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


def _parse_qacct_output(
    text: str,
    job_id: str,
    task_info: dict[int, dict],
    errors: list[dict],
) -> None:
    """Parse a full qacct stdout buffer, feeding each block to the processor."""
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        if raw_line.startswith("====="):
            if current:
                _process_qacct_block(current, job_id, task_info, errors)
                current = {}
            continue
        parts = raw_line.split(None, 1)
        if len(parts) == 2:
            current[parts[0]] = parts[1].strip()
    if current:
        _process_qacct_block(current, job_id, task_info, errors)


def query_sge(job_ids: list[str], user: str | None = None) -> dict:
    """Query SGE via qstat + qacct for array task states.

    qstat is a single batched call (it already reports all of ``$USER``'s
    jobs).  qacct doesn't robustly support multi-job queries, but we
    deduplicate and memoize so the same job ID is never polled twice in
    one tick.

    Returns ``{"tasks": {task_id: {state, exit_code, job_id}, ...},
    "errors": [{code, detail}, ...]}``.
    """
    if not job_ids:
        return {"tasks": {}, "errors": []}

    task_info: dict[int, dict] = {}
    errors: list[dict] = []
    sge_user = user or os.environ.get("USER", os.environ.get("USERNAME", ""))

    # Phase 1: single qstat call for running/pending tasks across all jobs.
    qstat_ok = False
    try:
        result = subprocess.run(
            ["qstat", "-u", sge_user],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            qstat_out = result.stdout
            qstat_ok = True
        else:
            qstat_out = ""
            errors.append(
                {
                    "code": "qstat_failed",
                    "detail": f"qstat exit {result.returncode}: {result.stderr.strip()}",
                }
            )
    except subprocess.TimeoutExpired as exc:
        qstat_out = ""
        errors.append({"code": "qstat_unavailable", "detail": f"timeout: {exc}"})
    except FileNotFoundError as exc:
        qstat_out = ""
        errors.append({"code": "qstat_unavailable", "detail": f"binary not found: {exc}"})

    job_id_set = {str(j) for j in job_ids}
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

    # Phase 2: qacct for finished tasks - dedupe job_ids to avoid repeat
    # subprocess calls for the same ID within a single poll.
    qacct_any_ok = False
    seen: set[str] = set()
    for job_id in job_ids:
        key = str(job_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            result = subprocess.run(
                ["qacct", "-j", key],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            errors.append({"code": "qacct_unavailable", "detail": f"job {key} timeout: {exc}"})
            continue
        except FileNotFoundError as exc:
            errors.append({"code": "qacct_unavailable", "detail": f"binary not found: {exc}"})
            # No point trying more job IDs if the binary is missing.
            break
        if result.returncode != 0:
            # qacct often exits nonzero for still-running jobs -- that's expected;
            # we don't record this as an error, but note it for diagnostics only
            # if no qacct call ever succeeds.
            continue
        qacct_any_ok = True
        _parse_qacct_output(result.stdout, key, task_info, errors)

    # If both qstat failed and no qacct call succeeded, surface as sge_unavailable
    # in addition to the individual tool errors.
    if not qstat_ok and not qacct_any_ok and not task_info:
        errors.append(
            {
                "code": "sge_unavailable",
                "detail": "both qstat and qacct failed or returned no data",
            }
        )

    return {"tasks": task_info, "errors": errors}
