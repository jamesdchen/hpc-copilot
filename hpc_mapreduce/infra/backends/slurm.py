"""SLURM backend — submits array jobs via sbatch."""

import os

from hpc_mapreduce.infra.backends import HPCBackend, register


@register("slurm")
class SlurmBackend(HPCBackend):
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
