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
    # On-disk SGE template extension is ``.sh`` (see
    # hpc_mapreduce/templates/sge/*.sh). Keep this in sync with what
    # ``get_template_path`` would return for the SGE branch — the
    # historical ``__init__.py:get_template_path`` value was ``.sh``,
    # not ``.sge``.
    template_ext = ".sh"
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

    # ------------------------------------------------------------------
    # B5-PR2 capability hooks — pure, scheduler-shape-only helpers.
    # See SlurmBackend for the design rationale.
    # ------------------------------------------------------------------

    @staticmethod
    def build_alive_check_cmd(job_ids: list[str]) -> str:
        """Shell command whose stdout lists ``__ALIVE__<jid>`` per live job.

        Key the marker on ``qstat -j``'s exit code, not on the pipeline
        tail.  ``qstat | head -1`` would always return 0 (head reads
        empty stdin successfully), making ``&& echo __ALIVE__`` fire for
        missing jobs and the alive check meaningless.
        """
        import shlex
        if not job_ids:
            return "true"
        return (
            "{ "
            + "; ".join(
                f"qstat -j {shlex.quote(jid)} >/dev/null 2>&1 && echo __ALIVE__{jid}"
                for jid in job_ids
            )
            + "; } || true"
        )

    @staticmethod
    def parse_alive_output(stdout: str, job_ids: list[str]) -> set[str]:
        """Extract job ids from ``__ALIVE__<jid>`` markers."""
        alive: set[str] = set()
        for line in stdout.splitlines():
            token = line.strip()
            if token.startswith("__ALIVE__"):
                alive.add(token.removeprefix("__ALIVE__"))
        return alive

    @staticmethod
    def stderr_log_path(
        remote_path: str, job_name: str, job_id: str, task_id: int
    ) -> str:
        """Cluster-side stderr path for SGE: ``<remote_path>/<job_name>.o<job_id>.<task_id>``.

        SGE uses the ``-j y`` join-stderr-into-stdout convention in the
        templates, so the ``.o`` (output) file holds both streams.
        """
        return f"{remote_path.rstrip('/')}/{job_name}.o{job_id}.{task_id}"

    @staticmethod
    def err_log_disk_path(
        log_dir: str, scratch_dir: str, job_name: str, job_id: str, task_id: int
    ) -> str:
        """Local-disk path used by ``status.get_err_log_paths`` for SGE."""
        import os
        return os.path.join(scratch_dir, f"{job_name}.o{job_id}.{task_id}")

    @staticmethod
    def query_jobs(
        job_ids: list[str],
        *,
        sge_user: str | None = None,
        slurm_cluster: str | None = None,
    ) -> dict:
        """Dispatch to ``query_sge`` for SGE job state.

        Unified signature across SGE and SLURM so reduce.status can call
        ``backend_cls.query_jobs(...)`` without an inline ladder. The
        unused kwarg is ignored (slurm_cluster is irrelevant for SGE).
        """
        from hpc_mapreduce.infra.backends.query import query_sge
        return query_sge(job_ids, user=sge_user)

    @staticmethod
    def inspect_cluster(
        cluster_name: str,
        cfg: dict,
        *,
        sacct_window_hours: int = 24,
        stress_alloc_mem_pct: float = 0.80,
        stress_cpu_load_frac: float = 0.80,
        runner=None,
    ):
        """Dispatch to :func:`_sge_inspect` for SGE.

        Unified signature with :meth:`SlurmBackend.inspect_cluster`; SGE
        ignores ``sacct_window_hours`` because it builds its node /
        co-tenant view from ``qstat`` snapshots, which are wall-clock
        snapshots rather than a historical window.
        """
        from hpc_mapreduce.infra.inspect import _sge_inspect
        return _sge_inspect(
            cluster_name,
            cfg,
            stress_alloc_mem_pct=stress_alloc_mem_pct,
            stress_cpu_load_frac=stress_cpu_load_frac,
            runner=runner,
        )

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
