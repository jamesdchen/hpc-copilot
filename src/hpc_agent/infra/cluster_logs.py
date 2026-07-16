"""SSH-driven per-task log tailing.

Both ``ops/monitor`` (the `logs` atom) and ``ops/recover`` (the
`failures` atom enriches failed tasks with their stderr tails) need
the same remote log-fetching loop. Living here means recover doesn't
reach into monitor.

Pure transport: SSH to the cluster head node, tail each task's
stderr file. Per-scheduler stderr-path templates live on the backend
classes (``infra.backends.<scheduler>.stderr_log_path``) — this
function is the retry-over-job-ids + SSH-stderr-classification shell
around them.

**Server-side fold (latency-elimination F5).** The probe used to cost one
``ssh_run`` *per (task, job_id) candidate* — a F×J cold-dial fan-out that
dominated ``logs``/``failures`` triage latency. It is now a SINGLE ``ssh_run``:
one POSIX-``sh`` script walks every task's newest-first candidate list on the
remote and emits a **sentinel-framed** section per task. The framing is the
positive-evidence half (docs/design/connection-broker.md): each section is
delimited by a per-invocation-nonce marker and the whole stream is closed by
:func:`~hpc_agent.infra.ssh_validation.wrap_with_ack`'s ack echo. A section
whose successor marker (or the closing ack) never arrives is read **SEVERED**,
never "no log / empty log" — a truncated channel that clips the stream after
file *k* leaves files *k+1..* reported as severed reads (``ssh_error``), so an
NAT idle-drop / reaped channel can never masquerade as "the task wrote no
stderr" (run-12 finding 24; engineering-principles enforcement-map row 3).

The helper is POSIX ``sh`` only (no shipped interpreter, no bashism): the login
shell is always present, so there is no interpreter-absence degrade path to
take — the fold is unconditional.
"""

from __future__ import annotations

import secrets
import shlex
from typing import Any

from hpc_agent.infra import remote
from hpc_agent.infra.ssh_validation import split_ack, wrap_with_ack

__all__ = ["fetch_task_logs"]

# Ack prefix closing the fused stream. Its ABSENCE (``split_ack`` → rc ``None``)
# is positive proof the remote shell never reached the trailing echo — the
# channel was severed / truncated mid-stream, so the tail sections it never
# framed are UNKNOWN, not "empty" (run-12 finding 24).
_LOGTAIL_ACK_PREFIX = "__HPC_LOGTAIL_ACK__="


def _new_nonce() -> str:
    """A fresh per-invocation framing nonce (hex).

    Factored out so a test can pin it (``monkeypatch.setattr(cluster_logs,
    "_new_nonce", lambda: "deadbeef")``) and build an expected framed stream.
    A random nonce makes a log line that coincidentally equals a section marker
    astronomically unlikely, so arbitrary stderr content cannot forge the frame.
    """
    return secrets.token_hex(8)


def fetch_task_logs(
    *,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    job_ids: list[str],
    scheduler: str,
    task_ids: list[int],
    lines: int = 50,
    job_task_spans: dict[str, tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """SSH to the cluster and tail each task's stderr log in ONE round-trip.

    Tries the most recent ``job_id`` first, falls back through earlier
    ones (matching :func:`hpc_agent.execution.mapreduce.reduce.status.get_err_log_paths`
    semantics). Returns one dict per task; missing logs surface as
    ``{"task_id": int, "missing": True}``. A task whose section was truncated
    off the stream (severed channel) surfaces as
    ``{"task_id": int, "missing": True, "ssh_error": <why>}`` — the read is
    UNKNOWN, never a settled "no log".

    *task_ids* are 0-based ``HpcTaskId`` (the domain space the report keys);
    ``stderr_log_path`` maps the JOB-LOCAL 0-based id to its 1-based
    ``ArrayIndex`` via ``to_array_index`` when building the on-disk
    filename. Path conventions (must stay aligned with the job templates),
    where ``<idx>`` is the job-local ``ArrayIndex``:

    * SGE:    ``<remote_path>/logs/<job_name>.o<job_id>.<idx>``
    * SLURM:  ``<remote_path>/logs/<job_name>_<job_id>_<idx>.err``

    **Waved runs need *job_task_spans*.** An over-cap sweep on an
    index-bounded backend is submitted as one LOCAL ``1-<size>`` array per
    batch plus a ``TASK_OFFSET`` (``backends.HPCBackend.submit_plan``,
    ``uses_global_array_index=False``), so the scheduler names each job's
    logs with the job-LOCAL index — probing a wave≥1 job with the global
    index either misses, or worse, matches ANOTHER task's log (task 5
    probed against wave 1's job hits the file of global task
    ``offset + 5``). *job_task_spans* maps ``job_id`` to the 0-based
    INCLUSIVE ``(first, last)`` global task-id window that job's array
    covers; a job with a span is probed only for tasks inside its window,
    using the local id ``tid - first``. Jobs absent from the map keep the
    global-index probe — correct for single-array (≤cap) runs and for
    resubmit arrays, which replay failed ids as GLOBAL array expressions
    (see ``ops/recover_flow``'s out-of-range guard).
    """
    if not task_ids:
        return []
    # B5-PR2: per-scheduler stderr-path templates live on the backend
    # class (``stderr_log_path``); this function is transport (SSH)
    # plus retry-over-job-ids only.
    from hpc_agent.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    spans = job_task_spans or {}

    # --- candidate resolution (unchanged per-task span logic; done up front
    #     so the ONE server-side script embeds every task's probe order) ------
    # ``candidates[tid]`` = newest-first ``[(job_id, path), ...]``; a task with
    # no covering job gets an empty list — it is resolved "missing" WITHOUT a
    # probe (no job's log dir could legitimately hold it), exactly as the old
    # per-file loop did (probed_any=False → missing).
    candidates: dict[int, list[tuple[str, str]]] = {}
    path_by_tid_job: dict[tuple[int, str], str] = {}
    for tid in task_ids:
        cands: list[tuple[str, str]] = []
        for job_id in reversed(job_ids or []):
            span = spans.get(job_id)
            if span is not None:
                first, last = span
                if not (first <= tid <= last):
                    # This job never ran this task: its log dir only holds
                    # OTHER tasks' logs under this filename scheme, so any
                    # probe hit would be a cross-task read.
                    continue
                local_tid = tid - first
            else:
                local_tid = tid
            path = backend_cls.stderr_log_path(remote_path, job_name, job_id, local_tid)
            cands.append((job_id, path))
            path_by_tid_job[(tid, str(job_id))] = path
        candidates[tid] = cands

    # Tasks that actually have somewhere to look — only these enter the script.
    script_tids = [tid for tid in task_ids if candidates[tid]]
    if not script_tids:
        # Nothing to probe anywhere: every task is genuinely un-covered. No SSH.
        return [{"task_id": tid, "missing": True} for tid in task_ids]

    nonce = _new_nonce()
    sec_marker = f"__HPC_LOGSEC_{nonce}"
    hit_marker = f"__HPC_LOGHIT_{nonce}"
    miss_marker = f"__HPC_LOGMISS_{nonce}"

    n = int(lines)
    parts: list[str] = []
    for tid in script_tids:
        # A leading ``\n`` on the section marker guarantees it lands on its own
        # line even when the previous ``tail`` output had no trailing newline —
        # so the frame is always parseable regardless of log content.
        parts.append(f"printf '\\n%s\\n' '{sec_marker} {tid}'")
        arms: list[str] = []
        for job_id, path in candidates[tid]:
            pq = shlex.quote(path)
            jq = shlex.quote(str(job_id))
            arms.append(f"if [ -f {pq} ]; then printf '{hit_marker} %s\\n' {jq}; tail -n {n} {pq};")
        body = " el".join(arms) + f" else printf '%s\\n' '{miss_marker}'; fi"
        parts.append(body)
    # A trailing newline so the ``wrap_with_ack`` echo lands on its own line
    # (a severed channel is the ONLY reason the ack goes missing).
    parts.append("printf '\\n'")

    script = wrap_with_ack("\n".join(parts), _LOGTAIL_ACK_PREFIX)
    proc = remote.ssh_run(script, ssh_target=ssh_target, op="fetch-task-logs")
    clean, ack_rc = split_ack(proc.stdout or "", _LOGTAIL_ACK_PREFIX)

    resolved = _parse_sections(
        clean,
        ack_present=ack_rc is not None,
        sec_marker=sec_marker,
        hit_marker=hit_marker,
        miss_marker=miss_marker,
        path_by_tid_job=path_by_tid_job,
    )

    if proc.returncode != 0:
        severed_why = (proc.stderr or "").strip()[-300:] or f"ssh exited {proc.returncode}"
    else:
        # rc-0 but a section was clipped off the stream (missing ack / missing
        # successor marker): the channel was severed mid-read. Positive-evidence
        # UNKNOWN, never "empty log".
        severed_why = (
            f"remote log stream severed: only {len(resolved)}/{len(script_tids)} task "
            f"sections framed before the stream truncated (run-12 finding 24); the "
            f"unframed tails are UNKNOWN, not empty"
        )

    out: list[dict[str, Any]] = []
    for tid in task_ids:
        if not candidates[tid]:
            # No covering job → genuinely missing, no probe was possible.
            out.append({"task_id": tid, "missing": True})
        elif tid in resolved:
            out.append(resolved[tid])
        else:
            # In the script but its section never arrived intact → SEVERED.
            out.append({"task_id": tid, "missing": True, "ssh_error": severed_why})
    return out


def _parse_sections(
    clean: str,
    *,
    ack_present: bool,
    sec_marker: str,
    hit_marker: str,
    miss_marker: str,
    path_by_tid_job: dict[tuple[int, str], str],
) -> dict[int, dict[str, Any]]:
    """Reduce the fused stream into ``{tid: result}`` for every INTACT section.

    A section is trusted only when its completeness is positively evidenced: a
    later section marker followed it, or the closing ack is present. When the
    ack is absent (``ack_present`` False) the LAST framed section may have been
    clipped mid-content, so it is dropped (left unresolved → the caller reports
    it severed) — the severed-frame honesty rule. Tasks whose section never
    appeared at all are simply absent from the result (also severed upstream).
    """
    lines = clean.split("\n")
    prefix = f"{sec_marker} "
    markers: list[tuple[int, int]] = []
    for idx, line in enumerate(lines):
        if line.startswith(prefix):
            raw = line[len(prefix) :].strip()
            try:
                markers.append((idx, int(raw)))
            except ValueError:
                continue

    # Section i's body runs to the next marker (or end of stream). A body is
    # complete iff a successor marker follows it, OR the ack closed the stream.
    complete_count = len(markers) if ack_present else max(0, len(markers) - 1)

    resolved: dict[int, dict[str, Any]] = {}
    for k, (idx, tid) in enumerate(markers):
        if k >= complete_count:
            break  # severed tail — not positively evidenced as complete.
        end = markers[k + 1][0] if k + 1 < len(markers) else len(lines)
        body = lines[idx + 1 : end]
        if not body:
            continue  # marker with no verdict line → treat as severed.
        head = body[0]
        if head == miss_marker:
            resolved[tid] = {"task_id": tid, "missing": True}
        elif head.startswith(f"{hit_marker} "):
            job_id = head[len(hit_marker) + 1 :].strip()
            path = path_by_tid_job.get((tid, job_id))
            # Trailing framing newlines (the successor marker's leading ``\n``,
            # the closing ``printf '\n'``) are stripped; internal newlines and
            # the tail's own line structure are preserved.
            content = "\n".join(body[1:]).rstrip("\n")
            resolved[tid] = {
                "task_id": tid,
                "path": path,
                "job_id": job_id,
                "content": content,
            }
        # Any other head is a malformed/clipped section → leave unresolved.
    return resolved
