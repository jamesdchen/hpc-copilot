"""Per-task log fetching."""

from __future__ import annotations

import shlex
from typing import Any

from claude_hpc.infra import remote
from claude_hpc.runner._ssh import _split_ssh_target


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
    ones (matching :func:`claude_hpc.mapreduce.reduce.status.get_err_log_paths`
    semantics). Returns one dict per task; missing logs surface as
    ``{"task_id": int, "missing": True}``.

    Path conventions (must stay aligned with the job templates):

    * SGE:    ``<remote_path>/<job_name>.o<job_id>.<task_id>``
    * SLURM:  ``<remote_path>/_hpc_logs/<job_name>_<job_id>_<task_id>.err``
    """
    if not task_ids:
        return []
    # B5-PR2: per-scheduler stderr-path templates live on the backend
    # class (``stderr_log_path``); this function is now transport (SSH)
    # plus retry-over-job-ids only.
    from claude_hpc.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    user, host = _split_ssh_target(ssh_target)
    out: list[dict[str, Any]] = []
    for tid in task_ids:
        found: dict[str, Any] | None = None
        for job_id in reversed(job_ids or []):
            path = backend_cls.stderr_log_path(remote_path, job_name, job_id, tid)
            quoted = shlex.quote(path)
            script = (
                f"if [ -f {quoted} ]; then "
                f"echo FOUND; tail -n {int(lines)} {quoted}; "
                f"else echo MISSING; fi"
            )
            proc = remote.ssh_run(script, host=host, user=user)
            if proc.returncode != 0:
                # SSH itself blew up; attribute to this attempt and try
                # the next job_id rather than aborting the whole batch.
                continue
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
        if found is None:
            out.append({"task_id": tid, "missing": True})
        else:
            out.append(found)
    return out
