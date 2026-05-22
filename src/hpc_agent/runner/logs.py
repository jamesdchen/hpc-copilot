"""Per-task log fetching."""

from __future__ import annotations

import shlex
from typing import Any

from hpc_agent.infra import remote


def fetch_task_logs(
    *,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    job_ids: list[str],
    scheduler: str,
    task_ids: list[int],
    lines: int = 50,
) -> list[dict[str, Any]]:
    """SSH to the cluster and tail each task's stderr log.

    Tries the most recent ``job_id`` first, falls back through earlier
    ones (matching :func:`hpc_agent.mapreduce.reduce.status.get_err_log_paths`
    semantics). Returns one dict per task; missing logs surface as
    ``{"task_id": int, "missing": True}``.

    Path conventions (must stay aligned with the job templates):

    * SGE:    ``<remote_path>/logs/<job_name>.o<job_id>.<task_id + 1>``
    * SLURM:  ``<remote_path>/logs/<job_name>_<job_id>_<task_id + 1>.err``
    """
    if not task_ids:
        return []
    # B5-PR2: per-scheduler stderr-path templates live on the backend
    # class (``stderr_log_path``); this function is now transport (SSH)
    # plus retry-over-job-ids only.
    from hpc_agent.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    out: list[dict[str, Any]] = []
    for tid in task_ids:
        found: dict[str, Any] | None = None
        ssh_error: str | None = None
        got_clean_response = False
        for job_id in reversed(job_ids or []):
            path = backend_cls.stderr_log_path(remote_path, job_name, job_id, tid)
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
            first, _, rest = stdout.partition("\n")
            if first.strip() == "FOUND":
                found = {
                    "task_id": tid,
                    "path": path,
                    "job_id": job_id,
                    "content": rest,
                }
                break
        if found is not None:
            out.append(found)
        elif got_clean_response:
            # The remote shell answered for at least one job_id and the
            # log genuinely was not there.
            out.append({"task_id": tid, "missing": True})
        else:
            # Every attempt hit an SSH transport error — do not let an
            # unreachable cluster masquerade as a merely-missing log.
            out.append(
                {"task_id": tid, "missing": True, "ssh_error": ssh_error or "ssh unreachable"}
            )
    return out
