"""Remote SLURM backend — submits array jobs via sbatch over SSH.

Mirrors :class:`claude_hpc.infra.backends.sge_remote.RemoteSGEBackend`
for SLURM clusters. Constructor takes a ``remote_repo`` path and an
``ssh_run`` callable; the local SLURM binary is never called.

Used by :func:`claude_hpc.orchestrator.submit_flow.submit_flow` when the spec's
``backend == "slurm"`` and the cluster's scheduler is SLURM (the common
case for hoffman2-class hosts is SGE, but discovery2 / many newer
clusters are SLURM).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc.infra.backends import register
from claude_hpc.infra.backends._remote_base import RemoteHPCBackend
from claude_hpc.infra.backends.slurm import SlurmBackend

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable


@register("slurm-remote")
class RemoteSlurmBackend(RemoteHPCBackend, SlurmBackend):
    """SLURM backend that runs sbatch on the cluster via SSH.

    Inherits ``_build_command``, ``_build_dependency_flag``, and
    ``JOB_ID_REGEX`` from :class:`SlurmBackend` — sbatch's command shape
    and output format are identical regardless of how it is invoked.
    The SSH-specific overrides (``_execute_command`` /
    ``_setup_log_dir``) come from :class:`RemoteHPCBackend`, listed
    first in the MRO so its overrides win.

    Parameters
    ----------
    script : str
        Path to the job script *on the remote host* (e.g.
        ``.hpc/templates/cpu_array.slurm``).
    ssh_run : callable
        A function ``(cmd: str) -> CompletedProcess`` that executes a
        shell command on the remote host via SSH.
    remote_repo : str
        Absolute path to the project directory on the remote host (used
        as ``cd`` target before ``sbatch``).
    log_dir : str | None
        Remote log directory. Defaults to ``<remote_repo>/logs``.
    account : str | None
        SLURM account to charge.
    cluster : str | None
        SLURM cluster name (passed via ``--clusters=`` for federated
        SLURM installations).
    """

    def __init__(
        self,
        script: str | None = None,
        ssh_run: Callable[[str], subprocess.CompletedProcess[str]] | None = None,
        remote_repo: str | None = None,
        log_dir: str | None = None,
        account: str | None = None,
        cluster: str | None = None,
    ):
        if ssh_run is None:
            raise ValueError("RemoteSlurmBackend requires an 'ssh_run' callable")
        if remote_repo is None:
            raise ValueError("RemoteSlurmBackend requires a 'remote_repo' path")
        # Default the remote log dir to <remote_repo>/logs before the
        # local SlurmBackend.__init__ sees None and falls back to the
        # local-machine SLURM_LOG_DIR env var.
        # SlurmBackend.__init__ env-falls-back to local SLURM_ACCOUNT /
        # SLURM_CLUSTER when its kwargs are None. That is wrong for a
        # remote backend (the local env is not the remote env), so we
        # bypass it: pass empty strings ("") through to disable the
        # fallback, then overwrite with the user's actual values.
        SlurmBackend.__init__(
            self,
            script=script,
            account="",
            cluster="",
            log_dir=log_dir or f"{remote_repo}/logs",
        )
        self.account = account or ""
        self.cluster = cluster or ""
        self.ssh_run = ssh_run
        self.remote_repo = remote_repo
