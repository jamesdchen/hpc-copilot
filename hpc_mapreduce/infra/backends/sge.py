"""SGE (Sun/Univa Grid Engine) backend — submits array jobs via qsub."""

import os
import re

from hpc_mapreduce.infra.backends import HPCBackend, register


@register("sge")
class SGEBackend(HPCBackend):
    # B5: capability metadata. SGE has no ``qsub --test-only``
    # equivalent so supports_test_only_eta stays False; the planner
    # falls back to the runtime prior alone.
    scheduler_name = "sge"
    template_ext = ".sge"
    supports_test_only_eta = False
    # qsub prints either ``Your job 12345 ("name") has been submitted``
    # (single jobs) or ``Your job-array 12345.1-10:1 ("name") has been
    # submitted``.  Anchor on that phrase so a stray digit elsewhere in
    # the output doesn't win.
    JOB_ID_REGEX = re.compile(r"Your job(?:-array)?\s+(\d+)")

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
        if extra_flags:
            cmd += extra_flags
        cmd.append(self.script)
        return cmd
