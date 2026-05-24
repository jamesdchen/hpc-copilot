"""Factory for SSH-backed scheduler clients (``RemoteSGEBackend`` /
``RemoteSlurmBackend``).

The submit and recover subjects both need to build the same remote
backend object: same SSH transport, same scheduler dispatch, same
env-key forwarding. Living here means neither subject reaches into
the other's source tree.

The function raises :class:`~hpc_agent.errors.SpecInvalid` on an
unknown scheduler name — the same typed envelope error the callers
surface to the agent. Callers MUST validate ``ssh_target`` before
calling (see :func:`hpc_agent.infra.remote.validate_ssh_target`); the
factory does not double-validate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.infra.backends.sge_remote import RemoteSGEBackend
from hpc_agent.infra.backends.slurm_remote import RemoteSlurmBackend
from hpc_agent.infra.remote import ssh_run

if TYPE_CHECKING:
    from hpc_agent.infra.backends import HPCBackend

__all__ = ["build_remote_backend"]


def build_remote_backend(
    *,
    backend_name: str,
    script: str,
    ssh_target: str,
    remote_path: str,
    pass_env_keys: tuple[str, ...] | None,
    job_env_keys: tuple[str, ...],
    slurm_account: str | None = None,
    slurm_cluster: str | None = None,
) -> HPCBackend:
    """Construct the right ``HPCBackend`` for the requested scheduler.

    Both SGE and SLURM go through the cluster's login node via SSH —
    the local backends (which assume a local ``qsub``/``sbatch``
    binary) are never used here. ``submit-flow`` and ``resubmit-flow``
    are both laptop-driven.
    """

    def ssh(cmd: str):
        return ssh_run(cmd, ssh_target=ssh_target)

    if backend_name == "sge":
        keys = pass_env_keys if pass_env_keys is not None else job_env_keys
        return RemoteSGEBackend(
            script=script,
            ssh_run=ssh,
            remote_repo=remote_path,
            pass_env_keys=tuple(keys),
        )
    if backend_name == "slurm":
        return RemoteSlurmBackend(
            script=script,
            ssh_run=ssh,
            remote_repo=remote_path,
            account=slurm_account,
            cluster=slurm_cluster,
        )
    raise errors.SpecInvalid(f"unknown backend: {backend_name!r}")
