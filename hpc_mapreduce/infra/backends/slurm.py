"""SLURM backend — submits array jobs via sbatch."""

import os
import re

from hpc_mapreduce.infra.backends import HPCBackend, register


@register("slurm")
class SlurmBackend(HPCBackend):
    # sbatch prints ``Submitted batch job 12345``.  Anchor on the phrase
    # so a warning prefix containing digits (``sbatch: warning: 30% of
    # nodes pre-empt; Submitted batch job 12345``) doesn't poison the
    # parse.
    JOB_ID_REGEX = re.compile(r"Submitted batch job\s+(\d+)")

    # B5: capability metadata — replaces ``if scheduler == "slurm"``
    # branches throughout the framework.
    scheduler_name = "slurm"
    template_ext = ".slurm"
    supports_test_only_eta = True

    def __init__(
        self,
        script: str | None = None,
        account: str | None = None,
        cluster: str | None = None,
        log_dir: str | None = None,
    ):
        if script is None:
            raise ValueError("SlurmBackend requires a 'script' path")
        self.script = script
        self.account = account or os.environ.get("SLURM_ACCOUNT", "")
        self.cluster = cluster or os.environ.get("SLURM_CLUSTER", "")
        self.log_dir = log_dir or os.environ.get("SLURM_LOG_DIR", "logs")

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
        cmd = [
            "sbatch",
        ]
        if self.cluster:
            cmd.append(f"--clusters={self.cluster}")
        cmd += [
            "--array",
            task_range,
            "--job-name",
            job_name,
        ]
        if self.account:
            cmd += ["--account", self.account]
        cmd += [
            "--output",
            f"{self.log_dir}/%x_%A_%a.out",
            "--error",
            f"{self.log_dir}/%x_%A_%a.err",
        ]
        if job_env:
            export_str = ",".join(f"{k}={v}" for k, v in job_env.items())
            cmd += ["--export", f"ALL,{export_str}"]
        if extra_flags:
            cmd += extra_flags
        cmd.append(self.script)
        return cmd
