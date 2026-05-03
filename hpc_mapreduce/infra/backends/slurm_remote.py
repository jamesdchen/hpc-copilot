"""Remote SLURM backend — submits array jobs via sbatch over SSH.

Mirrors :class:`hpc_mapreduce.infra.backends.sge_remote.RemoteSGEBackend`
for SLURM clusters. Constructor takes a ``remote_repo`` path and an
``ssh_run`` callable; the local SLURM binary is never called.

Used by :func:`hpc_mapreduce.job.submit_flow.submit_flow` when the spec's
``backend == "slurm"`` and the cluster's scheduler is SLURM (the common
case for hoffman2-class hosts is SGE, but discovery2 / many newer
clusters are SLURM).
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from hpc_mapreduce.infra.backends import HPCBackend, register

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path


@register("slurm-remote")
class RemoteSlurmBackend(HPCBackend):
    """SLURM backend that runs sbatch on the cluster via SSH.

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

    # Same anchored regex as the local SLURM backend — sbatch output
    # format is identical regardless of how we invoke it. A warning
    # prefix containing digits won't poison the parse.
    JOB_ID_REGEX = re.compile(r"Submitted batch job\s+(\d+)")

    def __init__(
        self,
        script: str | None = None,
        ssh_run: Callable[[str], subprocess.CompletedProcess[str]] | None = None,
        remote_repo: str | None = None,
        log_dir: str | None = None,
        account: str | None = None,
        cluster: str | None = None,
    ):
        if script is None:
            raise ValueError("RemoteSlurmBackend requires a 'script' path")
        if ssh_run is None:
            raise ValueError("RemoteSlurmBackend requires an 'ssh_run' callable")
        if remote_repo is None:
            raise ValueError("RemoteSlurmBackend requires a 'remote_repo' path")
        self.script = script
        self.ssh_run = ssh_run
        self.remote_repo = remote_repo
        self.log_dir = log_dir or f"{remote_repo}/logs"
        self.account = account
        self.cluster = cluster

    def _build_dependency_flag(self, job_ids: list[str]) -> list[str]:
        if not job_ids:
            return []
        return ["--dependency", f"afterany:{':'.join(job_ids)}"]

    def _build_command(
        self,
        task_range: str,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
    ) -> list[str]:
        """Return sbatch command parts for SSH execution."""
        parts: list[str] = ["sbatch"]
        if self.cluster:
            parts.append(f"--clusters={self.cluster}")
        parts += [
            "--array",
            task_range,
            "--job-name",
            job_name,
        ]
        if self.account:
            parts += ["--account", self.account]
        parts += [
            "--output",
            f"{self.log_dir}/%x_%A_%a.out",
            "--error",
            f"{self.log_dir}/%x_%A_%a.err",
        ]
        # SLURM forwards every key/value pair listed in --export. Unlike
        # SGE's qsub -v, there is no per-key whitelist; the cluster-side
        # template can read any name it expects.
        if job_env:
            export_str = ",".join(f"{k}={v}" for k, v in job_env.items())
            parts += ["--export", f"ALL,{export_str}"]
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
        """Execute an sbatch command on the remote host via SSH."""
        cmd_str = " ".join(shlex.quote(arg) for arg in cmd)
        remote_cmd = f"cd {shlex.quote(self.remote_repo)} && {cmd_str}"
        return self.ssh_run(remote_cmd)

    def _setup_log_dir(self) -> None:
        """Ensure the remote log directory exists via SSH ``mkdir -p``."""
        self.ssh_run(f"mkdir -p {shlex.quote(self.log_dir)}")
