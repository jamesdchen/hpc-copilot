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

**Task-id space (ingest edge).** This module is the scheduler-ingest
membrane: the ``JobId_N`` / ``taskid`` / ``ja-task-ID`` index every tool
reports back is a **1-based** :data:`~hpc_agent._kernel.contract.task_id.ArrayIndex`
(jobs submit ``--array=1-N``). Each parser routes it through
:func:`~hpc_agent._kernel.contract.task_id.to_task_id` so the ``tasks`` map
is keyed by **0-based** :data:`~hpc_agent._kernel.contract.task_id.HpcTaskId`
at the source — everything above the scheduler then speaks the same domain
identity the dispatcher's ``HPC_TASK_ID`` and ``resubmit_flow`` use, with no
compensating ``±1`` downstream.

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
    "query_pbs",
    "parse_gpu_count_from_tres",
    "parse_gpu_count_from_sge_resources",
]

import os
import re
import subprocess

from hpc_agent._kernel.contract.task_id import ArrayIndex, to_task_id
from hpc_agent.errors import SpecInvalid as _SpecInvalid
from hpc_agent.infra.parsing import parse_sacct_pipe_row
from hpc_agent.infra.parsing import to_int as _to_int

# sacct ``--format=`` list, kept here so the parser and the command
# string never drift out of sync.
_SACCT_QUERY_FORMAT: list[str] = [
    "JobID",
    "State",
    "ExitCode",
    "ElapsedRaw",
    "ReqCPUS",
    "AllocTRES",
]

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
    # Resubmits append the new array's job_id to the run record's
    # ``job_ids`` (oldest first → newest last). When the same task_id
    # appears in both old (failed) and new (running) arrays sacct may
    # return rows for both — prefer the row whose ``base_job`` is later
    # in the input list so the agent sees the most recent attempt
    # instead of the prior failure. Ties (same job_id) keep the
    # main-record-first dedup below.
    job_id_order: dict[str, int] = {str(j): i for i, j in enumerate(job_ids)}
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
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
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
        row = parse_sacct_pipe_row(parts, _SACCT_QUERY_FORMAT)
        job_field, state, exit_code = row["JobID"], row["State"], row["ExitCode"]
        # Optional trailing fields (only present when sacct was invoked with the
        # extended --format we request above).  Older fixtures/tests still pass.
        elapsed_raw = row["ElapsedRaw"]
        req_cpus = row["ReqCPUS"]
        alloc_tres = row["AllocTRES"]
        if "_" in job_field:
            # Array row: "12345_7" or "12345_7.batch"; strip trailing step.
            base_job, _, tail = job_field.partition("_")
            if base_job not in job_id_set:
                # sacct may return unrelated rows (shouldn't, but be defensive).
                continue
            # tail may be "7", "7.batch", "7.extern"; take the leading integer.
            tail = tail.split(".", 1)[0]
            # Ingest edge: the JobId_N index is a 1-based ArrayIndex; convert to
            # 0-based HpcTaskId so ``task_info`` is keyed in the domain space.
            try:
                tid = int(to_task_id(ArrayIndex(int(tail))))
            except (ValueError, _SpecInvalid):
                errors.append({"code": "malformed_row", "detail": f"non-integer task id: {line!r}"})
                continue
        else:
            # Non-array submission (#293, ``array=False`` MPI jobs): sacct
            # reports a plain "12345" main row plus "12345.batch"/".extern"
            # step rows. The run's single unit of work is task 0; the step
            # rows collapse into it via the same-array first-occurrence
            # dedup below (main record precedes steps in sacct output).
            base_job = job_field.split(".", 1)[0]
            if base_job not in job_id_set:
                # sacct may return unrelated rows (shouldn't, but be defensive).
                continue
            tid = 0
        # Dedup rule:
        #   * Within ONE array: first occurrence wins (main record comes
        #     before .batch/.extern steps for the same job_id).
        #   * Across MULTIPLE arrays for the same task_id (resubmit case):
        #     the row whose base_job appears later in the input job_ids
        #     list wins — that's the most recent attempt.
        existing = task_info.get(tid)
        if existing is not None:
            existing_pos = job_id_order.get(existing["job_id"], -1)
            new_pos = job_id_order.get(base_job, -1)
            if new_pos < existing_pos:
                # Older array's row arrived after the newer one's; keep newer.
                continue
            if new_pos == existing_pos:
                # Same array — first occurrence (main record) already kept.
                continue
            # new_pos > existing_pos — newer array, overwrite.
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
# PBS (PBS Pro / OpenPBS + TORQUE)
# ---------------------------------------------------------------------------

# PBS *live* job_state -> normalized state. Finished jobs (PBS Pro ``F`` /
# TORQUE ``C``) are NOT in this map: their success-vs-failure is read from
# ``Exit_status`` (0 -> COMPLETED, else FAILED), which is the one place PBS
# differs structurally from SLURM (where failure has its own state tokens).
_PBS_LIVE_STATE: dict[str, str] = {
    "R": "RUNNING",
    "E": "RUNNING",  # exiting/cleanup — not yet final (per pbs-drmaa)
    "B": "RUNNING",  # array has >=1 subjob running
    "Q": "PENDING",
    "W": "PENDING",
    "T": "PENDING",
    "H": "PENDING",  # held — waiting, not terminal
    "S": "PENDING",
    "U": "PENDING",
    "M": "PENDING",  # moved to another server
}


def _pbs_walltime_to_s(value: str) -> int:
    """Parse a PBS ``HH:MM:SS`` (or ``MM:SS``) duration to whole seconds."""
    parts = value.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    return 0


def _process_pbs_block(
    job_id_field: str,
    block: dict[str, str],
    task_info: dict[int, dict],
    *,
    job_pos: int = 0,
    task_pos: dict[int, int] | None = None,
) -> None:
    """Map one ``qstat -f`` subjob stanza to a task entry.

    Only array subjobs (``<seq>[<idx>].<server>``) yield an entry — the array
    parent (``<seq>[].<server>``) and non-array jobs carry no per-task index
    and are skipped. ``Exit_status`` (present only once finished) drives the
    success/failure split; otherwise the live ``job_state`` letter is mapped.

    *job_pos* / *task_pos* implement the resubmit dedup (mirroring
    :func:`query_sacct`): run records append resubmit job_ids oldest→newest,
    so when the same task appears under several jobs the stanza from the job
    LATER in the input list (higher *job_pos*) wins — that's the most recent
    attempt. Within one job (equal *job_pos*) the first stanza keeps winning.
    """
    m = re.match(r"^(\d+)\[(\d+)\]", job_id_field)
    if not m:
        return  # array parent ``[]`` / non-array job — no task index
    # Ingest edge: the PBS subjob ``[idx]`` is a 1-based ArrayIndex; convert
    # to 0-based HpcTaskId so ``task_info`` is keyed in the domain space.
    base_job = m.group(1)
    try:
        tid = int(to_task_id(ArrayIndex(int(m.group(2)))))
    except _SpecInvalid:
        return  # malformed (idx < 1) — skip rather than mis-key
    if task_pos is None:
        task_pos = {}
    existing_pos = task_pos.get(tid, job_pos if tid in task_info else None)
    if existing_pos is not None and existing_pos >= job_pos:
        return  # same job: first stanza wins; older job's stanza never overwrites

    exit_status = block.get("Exit_status")
    if exit_status is not None:
        try:
            ec = int(exit_status)
        except ValueError:
            ec = -1
        state = "COMPLETED" if ec == 0 else "FAILED"
        exit_code: str | None = exit_status
    else:
        state = _PBS_LIVE_STATE.get(block.get("job_state", "").strip(), "UNKNOWN")
        exit_code = None

    elapsed_s = _pbs_walltime_to_s(block.get("resources_used.walltime", ""))
    cpus = _to_int(block.get("resources_used.ncpus") or block.get("Resource_List.ncpus") or "")
    gpus = _to_int(block.get("resources_used.ngpus") or block.get("Resource_List.ngpus") or "")
    task_pos[tid] = job_pos
    task_info[tid] = {
        "state": state,
        "exit_code": exit_code,
        "job_id": base_job,
        "elapsed_s": elapsed_s,
        "cpu_s": cpus * elapsed_s,
        "gpu_s": gpus * elapsed_s,
    }


def _parse_qstat_full_pbs(
    text: str,
    task_info: dict[int, dict],
    *,
    job_pos: int = 0,
    task_pos: dict[int, int] | None = None,
) -> None:
    """Split a ``qstat -f`` buffer into ``Job Id:`` stanzas + feed each one.

    *job_pos* / *task_pos* thread the resubmit dedup state through to
    :func:`_process_pbs_block` (see its docstring for the precedence rule).
    """
    if task_pos is None:
        task_pos = {}
    current_id: str | None = None
    current: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("Job Id:"):
            if current_id is not None:
                _process_pbs_block(
                    current_id, current, task_info, job_pos=job_pos, task_pos=task_pos
                )
            current_id = line.split(":", 1)[1].strip()
            current = {}
            continue
        if current_id is None:
            continue
        # Attribute lines are ``    key = value`` (continuations of a wrapped
        # value are deeper-indented; our short fields never wrap).
        stripped = line.strip()
        if "=" in stripped and not raw.startswith(("\t\t", "        ")):
            key, _, val = stripped.partition("=")
            current[key.strip()] = val.strip()
    if current_id is not None:
        _process_pbs_block(current_id, current, task_info, job_pos=job_pos, task_pos=task_pos)


def query_pbs(job_ids: list[str], fork: str = "pbspro") -> dict:
    """Query PBS for array-subjob states via ``qstat -f`` (per job id).

    PBS Pro/OpenPBS needs ``-x`` to surface FINISHED jobs (``F``) and their
    ``Exit_status``; TORQUE retains a ``C`` record in plain ``qstat`` for a
    grace window, so it uses ``qstat -f``. ``-t`` expands array subjobs so
    each task gets its own stanza. Mirrors :func:`query_sge`'s per-id loop +
    uniform ``{"tasks", "errors"}`` return shape.
    """
    if not job_ids:
        return {"tasks": {}, "errors": []}

    task_info: dict[int, dict] = {}
    errors: list[dict] = []
    base_cmd = ["qstat", "-x", "-f", "-t"] if fork == "pbspro" else ["qstat", "-f", "-t"]

    # Resubmit dedup state: *job_ids* is ordered oldest→newest (resubmits
    # append), so recording which position wrote each task lets a later
    # (newer) job's stanza overwrite an earlier attempt's — see
    # :func:`_process_pbs_block`.
    task_pos: dict[int, int] = {}
    seen: set[str] = set()
    any_ok = False
    for job_pos, job_id in enumerate(job_ids):
        key = str(job_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            result = subprocess.run(
                [*base_cmd, key],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            errors.append({"code": "qstat_unavailable", "detail": f"job {key} timeout: {exc}"})
            continue
        except FileNotFoundError as exc:
            errors.append({"code": "qstat_unavailable", "detail": f"binary not found: {exc}"})
            break  # no point trying more ids if qstat is missing
        if result.returncode != 0:
            # Nonzero is common (history disabled, job aged out) — not fatal.
            continue
        any_ok = True
        _parse_qstat_full_pbs(result.stdout, task_info, job_pos=job_pos, task_pos=task_pos)

    if not any_ok and not task_info:
        errors.append({"code": "pbs_unavailable", "detail": "qstat returned no usable data"})
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
    if step <= 0:
        step = 1
    return list(range(start, end + 1, step))


def _process_qacct_block(
    block: dict[str, str],
    job_id: str,
    task_info: dict[int, dict],
    errors: list[dict],
    *,
    live_tids: frozenset[int] = frozenset(),
    job_pos: int = 0,
    task_pos: dict[int, int] | None = None,
) -> None:
    """Extract task status from a single qacct block.

    Precedence (two distinct rules — see :func:`query_sge`'s phases):

    * *live_tids* — tasks phase 1's live ``qstat`` already reported. Live
      queue data always beats accounting: a task running/pending under a
      resubmit must not be overwritten by a prior attempt's qacct record.
    * *job_pos* / *task_pos* — among accounting records, the resubmit dedup
      (mirroring :func:`query_sacct`): job_ids are ordered oldest→newest, so
      a job LATER in the input list (higher *job_pos*) overwrites an earlier
      attempt's record for the same task. Within one job (equal *job_pos*)
      the first block keeps winning.
    """
    tid_str = block.get("taskid", "")
    if not tid_str or tid_str == "undefined":
        return
    # Ingest edge: qacct ``taskid`` is a 1-based ArrayIndex; convert to
    # 0-based HpcTaskId so ``task_info`` is keyed in the domain space.
    try:
        tid = int(to_task_id(ArrayIndex(int(tid_str))))
    except (ValueError, _SpecInvalid):
        errors.append({"code": "malformed_row", "detail": f"qacct non-integer taskid: {tid_str!r}"})
        return
    if tid in live_tids:
        return  # live qstat data takes precedence over accounting
    if task_pos is None:
        task_pos = {}
    existing_pos = task_pos.get(tid, job_pos if tid in task_info else None)
    if existing_pos is not None and existing_pos >= job_pos:
        return  # same job: first block wins; older job's block never overwrites

    exit_status = block.get("exit_status", "0")
    failed = block.get("failed", "0")
    parse_failed = False
    try:
        exit_int = int(exit_status)
        failed_tokens = failed.split() if failed else []
        failed_int = int(failed_tokens[0]) if failed_tokens else 0
    except ValueError:
        errors.append(
            {
                "code": "malformed_row",
                "detail": f"qacct non-integer exit/failed for job {job_id}",
            }
        )
        exit_int, failed_int = -1, -1
        parse_failed = True

    if parse_failed:
        # Unparseable exit/failed fields: classify as a generic FAILED
        # rather than letting the -1 sentinel fall through into the
        # ``failed_int != 0`` branch and alarm with a (wrong) NODE_FAIL.
        state = "FAILED"
    elif exit_int == 0 and failed_int == 0:
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

    task_pos[tid] = job_pos
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
    *,
    live_tids: frozenset[int] = frozenset(),
    job_pos: int = 0,
    task_pos: dict[int, int] | None = None,
) -> None:
    """Parse a full qacct stdout buffer, feeding each block to the processor.

    The keyword args thread the two-phase precedence state through to
    :func:`_process_qacct_block` (see its docstring for the rules).
    """
    if task_pos is None:
        task_pos = {}
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        if raw_line.startswith("====="):
            if current:
                _process_qacct_block(
                    current,
                    job_id,
                    task_info,
                    errors,
                    live_tids=live_tids,
                    job_pos=job_pos,
                    task_pos=task_pos,
                )
                current = {}
            continue
        parts = raw_line.split(None, 1)
        if len(parts) == 2:
            current[parts[0]] = parts[1].strip()
    if current:
        _process_qacct_block(
            current,
            job_id,
            task_info,
            errors,
            live_tids=live_tids,
            job_pos=job_pos,
            task_pos=task_pos,
        )


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
    sge_user = user or os.environ.get("USER") or os.environ.get("USERNAME") or ""
    if not sge_user:
        # Refuse to call ``qstat -u ""`` — SGE interprets that
        # inconsistently across versions (some treat it as "no filter",
        # which returns the entire cluster's queue). Caller passed no
        # user and the shell has no $USER/$USERNAME; we cannot identify
        # which jobs belong to whom.
        # Error-list entry shape is ``{"code", "detail"}`` per the
        # module's documented contract (line 13).
        return {
            "tasks": {},
            "errors": [
                {
                    "code": "qstat_unavailable",
                    "detail": "no user identity ($USER/$USERNAME unset and no `user=` arg)",
                }
            ],
        }

    # Phase 1: single qstat call for running/pending tasks across all jobs.
    qstat_ok = False
    try:
        result = subprocess.run(
            ["qstat", "-u", sge_user],
            capture_output=True,
            text=True,
            encoding="utf-8",
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
        # qstat -u <user> trailing columns after the date/time pair are
        # [queue] slots [ja-task-ID]: running jobs carry the queue
        # column, pending jobs don't, and only array jobs carry the
        # task-ID. Reading cols[-1] unconditionally mistakes a non-array
        # running job's slot count for a task range. Disambiguate from
        # the tail shape — the slots column is always an integer, the
        # queue column never is.
        tail = cols[7:]
        task_spec = ""
        if len(tail) == 2 and tail[0].isdigit():
            task_spec = tail[1].strip()  # pending array: slots task
        elif len(tail) >= 3:
            task_spec = tail[2].strip()  # running array: queue slots task
        for array_idx in _expand_task_range(task_spec):
            # Ingest edge: qstat ja-task-IDs are 1-based ArrayIndexes;
            # convert to 0-based HpcTaskId so ``task_info`` is domain-keyed.
            try:
                tid = int(to_task_id(ArrayIndex(array_idx)))
            except _SpecInvalid:
                continue
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
    # subprocess calls for the same ID within a single poll. Two precedence
    # rules apply (threaded into _process_qacct_block):
    #   * live qstat data (phase 1) beats accounting data, and
    #   * among accounting records, the job LATER in *job_ids* wins per task
    #     (resubmits append oldest→newest, so later = the newest attempt).
    live_tids = frozenset(task_info)
    task_pos: dict[int, int] = {}
    qacct_any_ok = False
    seen: set[str] = set()
    for job_pos, job_id in enumerate(job_ids):
        key = str(job_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            result = subprocess.run(
                ["qacct", "-j", key],
                capture_output=True,
                text=True,
                encoding="utf-8",
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
        _parse_qacct_output(
            result.stdout,
            key,
            task_info,
            errors,
            live_tids=live_tids,
            job_pos=job_pos,
            task_pos=task_pos,
        )

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
