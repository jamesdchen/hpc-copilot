"""Shared SSH-shim mixin for remote scheduler backends.

Both :class:`hpc_agent.infra.backends.sge_remote.RemoteSGEBackend`
and :class:`hpc_agent.infra.backends.slurm_remote.RemoteSlurmBackend`
need the exact same two overrides on top of their local cousin:

- ``_execute_command`` — wrap the scheduler invocation in
  ``cd <remote_repo> && <cmd>`` and run it via the injected ``ssh_run``
  callable.
- ``_setup_log_dir`` — ``mkdir -p`` the remote log dir over SSH.

This module exposes :class:`RemoteHPCBackend` as a mixin (placed FIRST
in the MRO) so each Remote backend simply does::

    class RemoteSGEBackend(RemoteHPCBackend, SGEBackend): ...

and inherits ``_build_command`` / ``_build_dependency_flag`` /
``JOB_ID_REGEX`` from the local class while overriding the two SSH
hooks here.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path


class RemoteHPCBackend:
    """SSH shim for scheduler backends.

    Subclasses are expected to set the following instance attributes
    (typically in their ``__init__`` via ``super().__init__(...)``):

    - ``ssh_run`` — ``Callable[[str], subprocess.CompletedProcess[str]]``
    - ``remote_repo`` — absolute path on the remote host
    - ``log_dir`` — remote log directory
    """

    ssh_run: Callable[[str], subprocess.CompletedProcess[str]]
    remote_repo: str
    log_dir: str

    def _execute_command(
        self,
        cmd: list[str],
        job_env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Execute *cmd* on the remote host via SSH.

        ``cd <remote_repo> && <quoted-cmd>`` is the canonical pattern —
        the job script is referenced by relative path in the local
        backend's ``_build_command``, so we need to land in the right
        directory before invoking qsub/sbatch.
        """
        cmd_str = " ".join(shlex.quote(arg) for arg in cmd)
        remote_cmd = f"cd {shlex.quote(self.remote_repo)} && {cmd_str}"
        return self.ssh_run(remote_cmd)

    def _setup_log_dir(self) -> None:
        """Create the log directory on the remote host via SSH."""
        self.ssh_run(f"mkdir -p {shlex.quote(self.log_dir)}")
