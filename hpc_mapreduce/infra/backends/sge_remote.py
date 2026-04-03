"""Remote SGE backend — submits array jobs via qsub over SSH.

This backend requires a ``remote_repo`` path and an ``ssh_run`` callable
to be provided at construction time (no project-specific imports).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_mapreduce.infra.backends import HPCBackend, register

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path


@register("sge-remote")
class RemoteSGEBackend(HPCBackend):
    """SGE backend that runs qsub on the cluster via SSH.

    Unlike the local ``SGEBackend``, this backend does not call ``qsub``
    directly — it wraps each command in ``ssh_run()`` so submissions
    happen on a remote login node.

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
        if script is None:
            raise ValueError("RemoteSGEBackend requires a 'script' path")
        if ssh_run is None:
            raise ValueError("RemoteSGEBackend requires an 'ssh_run' callable")
        if remote_repo is None:
            raise ValueError("RemoteSGEBackend requires a 'remote_repo' path")
        self.script = script
        self.ssh_run = ssh_run
        self.remote_repo = remote_repo
        self.log_dir = log_dir or f"{remote_repo}/logs"
        self.pass_env_keys = pass_env_keys

    def _build_dependency_flag(self, job_ids: list[str]) -> list[str]:
        if not job_ids:
            return []
        return ["-hold_jid", ",".join(job_ids)]

    def _build_command(
        self,
        task_range: str,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
    ) -> list[str]:
        """Return qsub command parts for SSH execution."""
        parts = [
            "qsub",
            "-t",
            task_range,
            "-N",
            job_name,
            "-o",
            self.log_dir,
            "-j",
            "y",
        ]
        pass_vars = ",".join(f"{k}={v}" for k, v in job_env.items() if k in self.pass_env_keys)
        if pass_vars:
            parts += ["-v", pass_vars]
        if extra_flags:
            parts += extra_flags
        parts.append(self.script)
        return parts

    def _execute_command(
        self,
        cmd: list[str],
        job_env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a qsub command on the remote host via SSH."""
        cmd_str = " ".join(cmd)
        remote_cmd = f"cd {self.remote_repo} && {cmd_str}"
        return self.ssh_run(remote_cmd)

    def _setup_log_dir(self) -> None:
        """Create log directory on the remote host."""
        self.ssh_run(f"mkdir -p {self.log_dir}")
