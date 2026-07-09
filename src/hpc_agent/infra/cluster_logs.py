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
"""

from __future__ import annotations

import shlex
from typing import Any

from hpc_agent.infra import remote

__all__ = ["fetch_task_logs"]


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
    """SSH to the cluster and tail each task's stderr log.

    Tries the most recent ``job_id`` first, falls back through earlier
    ones (matching :func:`hpc_agent.execution.mapreduce.reduce.status.get_err_log_paths`
    semantics). Returns one dict per task; missing logs surface as
    ``{"task_id": int, "missing": True}``.

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
    out: list[dict[str, Any]] = []
    for tid in task_ids:
        found: dict[str, Any] | None = None
        ssh_error: str | None = None
        got_clean_response = False
        probed_any = False
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
            probed_any = True
            path = backend_cls.stderr_log_path(remote_path, job_name, job_id, local_tid)
            quoted = shlex.quote(path)
            script = (
                f"if [ -f {quoted} ]; then "
                f"echo FOUND; tail -n {int(lines)} {quoted}; "
                f"else echo MISSING; fi"
            )
            proc = remote.ssh_run(script, ssh_target=ssh_target)
            if proc.returncode != 0:
                # SSH transport itself blew up; record it and try the
                # next job_id rather than aborting the whole batch.
                ssh_error = (proc.stderr or "").strip()[-300:] or f"ssh exited {proc.returncode}"
                continue
            got_clean_response = True
            stdout = proc.stdout or ""
            first_line, _, rest = stdout.partition("\n")
            if first_line.strip() == "FOUND":
                found = {
                    "task_id": tid,
                    "path": path,
                    "job_id": job_id,
                    "content": rest,
                }
                break
        if found is not None:
            out.append(found)
        elif got_clean_response or not probed_any:
            # The remote shell answered for at least one job_id and the log
            # genuinely was not there — or no job's span covered this task,
            # so no probe could legitimately find it.
            out.append({"task_id": tid, "missing": True})
        else:
            # Every attempt hit an SSH transport error — do not let an
            # unreachable cluster masquerade as a merely-missing log.
            out.append(
                {"task_id": tid, "missing": True, "ssh_error": ssh_error or "ssh unreachable"}
            )
    return out
