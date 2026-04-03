"""SGE (Sun/Univa Grid Engine) backend — submits array jobs via qsub."""

import os

from hpc_mapreduce.infra.backends import HPCBackend, register


@register("sge")
class SGEBackend(HPCBackend):
    def __init__(
        self,
        script: str | None = None,
        log_dir: str | None = None,
        pass_env_keys: tuple[str, ...] = (),
    ):
        if script is None:
            raise ValueError("SGEBackend requires a 'script' path")
        self.script = script
        self.log_dir = log_dir or os.environ.get("SGE_LOG_DIR", "logs")
        self.pass_env_keys = pass_env_keys

    def _build_command(self, task_range: str, job_name: str, job_env: dict[str, str]) -> list[str]:
        cmd = [
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
            cmd += ["-v", pass_vars]
        cmd.append(self.script)
        return cmd
