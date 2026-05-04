"""Scheduler-specific job status queries (SGE and SLURM).

Batched-poll variant: each call to :func:`query_sacct` / :func:`query_sge`
spawns **at most one** subprocess per scheduler tool rather than one per
job ID.  This drastically reduces the number of SSH round-trips the
/status loop incurs when many waves x batches are in flight.

Return shape (uniform across happy / error paths)::

    {
        "tasks": {task_id: {"state": str, "exit_code": str | None, "job_id": str,
                             "elapsed_s": int, "cpu_s": int, "gpu_s": int}, ...},
        "errors": [{"code": str, "detail": str}, ...],
    }

The three resource-usage keys (``elapsed_s``, ``cpu_s``, ``gpu_s``) are
**always present** on every task dict so callers can sum them without
defensively checking for missing keys.  Values are 0 when the scheduler
does not yet know (e.g. still-running task reported by qstat).

- Happy path: ``errors`` is ``[]``.
- Tool missing / timeout: ``tasks`` is ``{}`` and ``errors`` has one entry.
- Partial failures: both populated.
"""

from __future__ import annotations

__all__ = [
    "query_sacct",
    "query_sge",
    "parse_gpu_count_from_tres",
    "parse_gpu_count_from_sge_resources",
]

import os
import re
import subprocess

# ---------------------------------------------------------------------------
# Resource-usage parsing helpers
# ---------------------------------------------------------------------------


def parse_gpu_count_from_tres(tres: str) -> int:
    """Parse GPU count from a SLURM TRES-style string.

    Accepts values like ``"cpu=4,mem=16G,gres/gpu=2"`` or
    ``"gres/gpu:a100=1,cpu=8"``.  Permissive: returns 0 on unrecognized
    input rather than raising.  Only ``gres/gpu`` entries are counted.
    """
    if not tres:
        return 0
    total = 0
    for part in tres.split(","):
        part = part.strip()
        if not part.startswith("gres/gpu"):
            continue
        # accepted forms: gres/gpu=N, gres/gpu:type=N
        _, _, rhs = part.partition("=")
        rhs = rhs.strip()
        if not rhs:
            continue
        # strip trailing unit-ish suffix (rare for gpu but defensive)
        m = re.match(r"(\d+)", rhs)
        if not m:
            continue
        try:
            total += int(m.group(1))
        except ValueError:
            continue
    return total


def parse_gpu_count_from_sge_resources(text: str) -> int:
    """Parse GPU count from SGE-style resource / complex text.

    Handles snippets like ``hard resource_list: h_rt=3600,gpu=2`` or
    ``qsub_arg_list: -l gpu=1``.  Permissive: returns 0 on unrecognized
    input.  Matches ``gpu=N`` case-insensitively, anywhere in the string.
    """
    if not text:
        return 0
    total = 0
    # gpu=N (also matches num_gpu=N, cuda_gpu=N -- keep narrow to avoid FPs)
    for m in re.finditer(r"(?<![A-Za-z_])gpu\s*=\s*(\d+)", text, flags=re.IGNORECASE):
        try:
            total += int(m.group(1))
        except ValueError:
            continue
    return total


def _to_int(value: str | None, default: int = 0) -> int:
    """Best-effort int parse.  Returns *default* on any failure."""
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    # tolerate trailing ".0" from some sacct formats
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return default


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

    cmd = [
        "sacct",
        "-j",
        joined,
        "--format=JobID,State,ExitCode,ElapsedRaw,ReqCPUS,AllocTRES",
        "--noheader",
        "--parsable2",
    ]
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
        # Optional trailing fields (only present when sacct was invoked with the
        # extended --format we request above).  Older fixtures/tests still pass.
        elapsed_raw = parts[3] if len(parts) > 3 else ""
        req_cpus = parts[4] if len(parts) > 4 else ""
        alloc_tres = parts[5] if len(parts) > 5 else ""
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
        elapsed_s = _to_int(elapsed_raw)
        cpus = _to_int(req_cpus)
        gpus = parse_gpu_count_from_tres(alloc_tres)
        task_info[tid] = {
            "state": state,
            "exit_code": exit_code,
            "job_id": base_job,
            "elapsed_s": elapsed_s,
            "cpu_s": cpus * elapsed_s,
            "gpu_s": gpus * elapsed_s,
        }

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

    # ru_wallclock is seconds (can be float); slots is the CPU count.
    elapsed_s = _to_int(block.get("ru_wallclock", ""))
    slots = _to_int(block.get("slots", ""), default=1) or 1
    # GPUs may appear on "hard resource_list" or "qsub_arg_list" lines.
    gpu_text = " ".join(
        v for k, v in block.items() if k in ("hard", "resource_list", "qsub_arg_list", "granted_pe")
    )
    gpus = parse_gpu_count_from_sge_resources(gpu_text)

    task_info[tid] = {
        "state": state,
        "exit_code": exit_status,
        "job_id": job_id,
        "elapsed_s": elapsed_s,
        "cpu_s": slots * elapsed_s,
        "gpu_s": gpus * elapsed_s,
    }


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
            task_info[tid] = {
                "state": normalized,
                "exit_code": None,
                "job_id": jid,
                # qstat does not report resource usage for live tasks; leave 0
                # so the per-task dict shape is uniform with qacct results.
                "elapsed_s": 0,
                "cpu_s": 0,
                "gpu_s": 0,
            }

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
