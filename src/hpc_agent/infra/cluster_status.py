"""SSH-driven cluster status reporter.

Both ``ops/monitor`` (`poll-run-status`), ``ops/aggregate`` (the
`aggregate-flow` composite reads status before reduce), and
``ops/recover`` (`failures` atom enriches failures with status) need
the same remote-side status JSON. Living here means none of those
subjects reach into another to fetch it.

The function shells out via :func:`hpc_agent.infra.remote.ssh_run`
to ``python -m hpc_agent.execution.mapreduce.reduce.status`` on the
cluster head node, parses the returned JSON, and raises
:class:`~hpc_agent.errors.RemoteCommandFailed` on transport or
parse failure.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from hpc_agent.errors import RemoteCommandFailed, SshUnreachable
from hpc_agent.infra import remote
from hpc_agent.infra.ssh_validation import parse_remote_json

if TYPE_CHECKING:
    from hpc_agent.infra.backends import HPCBackend

__all__ = ["ssh_status_report", "ssh_batch_scheduler_states"]


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
    remote_activation: str = "",
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
        f"{remote_activation}"
        f"python -m hpc_agent.execution.mapreduce.reduce.status "
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


def ssh_batch_scheduler_states(
    *,
    ssh_target: str,
    backend_cls: type[HPCBackend],
    job_ids: list[str],
) -> dict[str, str]:
    """ONE scheduler query for *all* of *job_ids*; return ``{job_id: raw_state}``.

    The batched counterpart of :func:`ssh_status_report`: instead of running
    the on-cluster reporter per run, this issues a single
    ``qstat -u $USER`` (SGE/PBS) / ``squeue`` (SLURM) over one SSH connection
    and parses every requested job id's raw scheduler state at once — the
    connection-storm fix (Nextflow/Parsl query the scheduler once for all
    jobs, not once per job). The same login node, the same query, regardless
    of how many runs share it.

    Returns the raw-token map (``parse_scheduler_states`` shape); the caller
    runs ``backend_cls.batch_status`` to fold tokens into ``TaskStatus``
    values. Job ids absent from the scheduler output are omitted (they have
    left the queue — terminal). Raises :class:`SshUnreachable` on an SSH
    transport failure (the state commands append ``|| true`` so a reachable
    cluster always returns rc 0; a non-zero rc is transport, not "no jobs").
    """
    if not job_ids:
        return {}
    cmd = backend_cls.build_scheduler_state_cmd(job_ids)
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise SshUnreachable(
            f"batch scheduler-state query failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )
    return backend_cls.parse_scheduler_states(proc.stdout, job_ids)
