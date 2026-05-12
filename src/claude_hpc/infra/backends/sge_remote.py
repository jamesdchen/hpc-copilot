"""Remote SGE backend — submits array jobs via qsub over SSH.

This backend requires a ``remote_repo`` path and an ``ssh_run`` callable
to be provided at construction time (no project-specific imports).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc.infra.backends import register
from claude_hpc.infra.backends._remote_base import RemoteHPCBackend
from claude_hpc.infra.backends.sge import SGEBackend

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable


@register("sge")
class RemoteSGEBackend(RemoteHPCBackend, SGEBackend):
    """SGE backend that runs qsub on the cluster via SSH.

    Inherits ``_build_command``, ``_build_dependency_flag``, and
    ``JOB_ID_REGEX`` from :class:`SGEBackend` — qsub's command shape and
    output format are identical regardless of how it is invoked. The
    SSH-specific overrides (``_execute_command`` / ``_setup_log_dir``)
    come from :class:`RemoteHPCBackend`, listed first in the MRO so its
    overrides win.

    Parameters
    ----------
    script : str
        Path to the job script *on the remote host*.
    ssh_run : callable
        A function ``(cmd: str) -> CompletedProcess`` that executes a
        shell command on the remote host via SSH.
    remote_repo : str
        Absolute path to the project directory on the remote host (used
        as ``cd`` target before ``qsub``).
    log_dir : str | None
        Remote log directory.  Defaults to ``<remote_repo>/logs``.
    pass_env_keys : tuple[str, ...]
        Environment variable names to forward via ``qsub -v``.
    """

    def __init__(
        self,
        script: str | None = None,
        ssh_run: Callable[[str], subprocess.CompletedProcess[str]] | None = None,
        remote_repo: str | None = None,
        log_dir: str | None = None,
        pass_env_keys: tuple[str, ...] = (),
    ):
        if ssh_run is None:
            raise ValueError("RemoteSGEBackend requires an 'ssh_run' callable")
        if remote_repo is None:
            raise ValueError("RemoteSGEBackend requires a 'remote_repo' path")
        # Default the remote log dir to <remote_repo>/logs before the
        # local SGEBackend.__init__ sees None and falls back to the
        # local-machine SGE_LOG_DIR env var (which is wrong on a remote).
        SGEBackend.__init__(
            self,
            script=script,
            log_dir=log_dir or f"{remote_repo}/logs",
            pass_env_keys=pass_env_keys,
        )
        self.ssh_run = ssh_run
        self.remote_repo = remote_repo
