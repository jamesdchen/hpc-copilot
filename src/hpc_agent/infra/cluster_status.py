"""SSH-driven cluster status reporter.

Both ``ops/monitor`` (`poll-run-status`), ``ops/aggregate`` (the
`aggregate-flow` composite reads status before reduce), and
``ops/recover`` (`failures` atom enriches failures with status) need
the same remote-side status JSON. Living here means none of those
subjects reach into another to fetch it.

The function shells out via :func:`hpc_agent.infra.remote.ssh_run`
to ``python -m hpc_agent.models.mapreduce.reduce.status`` on the
cluster head node, parses the returned JSON, and raises
:class:`~hpc_agent.errors.RemoteCommandFailed` on transport or
parse failure.
"""

from __future__ import annotations

import shlex

from hpc_agent.errors import RemoteCommandFailed
from hpc_agent.infra import remote
from hpc_agent.infra.remote import parse_remote_json

__all__ = ["ssh_status_report"]


def ssh_status_report(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    job_ids: list[str],
    job_name: str,
    log_dir: str = "logs",
    file_glob: str = "*",
    min_rows: int = 0,
) -> dict:
    """Run the on-cluster status reporter (``--run-id``) and return parsed JSON.

    The reporter reads ``.hpc/runs/<run_id>.json`` for run metadata
    and ``.hpc/tasks.py`` for per-task kwargs, then emits the JSON
    envelope pinned by ``docs/reference/python-api-contract.md``
    (summary / tasks / rollup / errors).

    ``min_rows`` is forwarded to the cluster-side reporter's
    ``--min-rows`` flag: a completed task whose CSV result has fewer
    than ``min_rows`` data rows beyond the header is demoted
    ``complete`` → ``failed``. The default ``0`` accepts header-only
    CSVs (legitimately-empty results).
    """
    job_ids_csv = ",".join(job_ids)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"python -m hpc_agent.models.mapreduce.reduce.status "
        f"--run-id {shlex.quote(run_id)} "
        f"--job-ids {shlex.quote(job_ids_csv)} "
        f"--job-name {shlex.quote(job_name)} "
        f"--log-dir {shlex.quote(log_dir)} "
        f"--file-glob {shlex.quote(file_glob)} "
        f"--min-rows {shlex.quote(str(int(min_rows)))}"
    )
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"status reporter failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    return parse_remote_json(proc.stdout, source_label="status reporter")
