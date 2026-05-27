"""SGE (Sun/Univa Grid Engine) backend — submits array jobs via qsub.

The wire-facing ``backend`` value ``"sge"`` resolves to
:class:`hpc_agent.infra.backends.sge_remote.RemoteSGEBackend` (the
remote-over-ssh subclass). This local class is no longer registered
because nothing in src/ or tests/ submits jobs from a local SGE shell —
every submission flows through the SSH boundary. It remains as a base
class for the remote subclass (which inherits its capability metadata
and parser regexes).
"""

import os
import re

from hpc_agent import errors
from hpc_agent.infra.backends import HPCBackend


class SGEBackend(HPCBackend):
    # B5: capability metadata. SGE has no ``qsub --test-only``
    # equivalent so supports_test_only_eta stays False; the planner
    # falls back to the runtime prior alone.
    scheduler_name = "sge"
    # On-disk SGE template extension is ``.sh`` (see
    # hpc_agent/templates/runtime/sge/*.sh). Keep this in sync with what
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
            raise errors.SpecInvalid("SGEBackend requires a 'script' path")
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
        """Shell command whose stdout lists active SGE job ids for ``$USER``.

        Single cluster-side ``qstat -u $USER`` call regardless of N.
        Previously we emitted one ``qstat -j <jid>`` invocation per job
        id and chained them with ``;`` — for a 100-task array polled
        every 30s over 4h that's 48k subprocess spawns on the head node.

        Why ``qstat -u $USER`` over ``qstat -j ID1,ID2,...``:
          * ``qstat -j`` is documented (and behaves) as accepting a
            single ``job_identifier``; comma-separated lists are NOT
            portable across SGE forks (Univa / Son of Grid Engine /
            OGS) — some accept it, some treat the whole string as one
            opaque id and report "Following jobs do not exist".
          * For very long id lists, multi-arg ``qstat -j ID1 ID2 ...``
            risks blowing ``ARG_MAX`` on the wrapping ssh/exec call.
          * ``qstat -u $USER`` is already the pattern used by
            :func:`hpc_agent.infra.backends.query.query_sge` (see
            ``query.py:_SGE`` block) so the output shape and parser
            convention is well-trodden.

        The filtering happens in :meth:`parse_alive_output`; this keeps
        the on-the-wire payload tiny (no per-id marker echoing).
        ``|| true`` ensures rc 0 on an empty-user-queue so the SSH
        transport guard in reconcile doesn't mistake "no live jobs"
        for "transport failure".
        """
        if not job_ids:
            return "true"
        # NB: $USER expands cluster-side, matching query_sge's invocation.
        return 'qstat -u "$USER" 2>/dev/null || true'

    @staticmethod
    def parse_alive_output(stdout: str, job_ids: list[str]) -> set[str]:
        """Filter ``qstat -u $USER`` output to the requested *job_ids*.

        ``qstat -u`` output has a 2-line header (``job-ID prior name ...``
        and ``---...---``) followed by one row per job; the job id is the
        first whitespace-separated column. Lines that don't start with a
        digit (headers, blanks) are skipped silently.
        """
        alive: set[str] = set()
        wanted = {str(j) for j in job_ids}
        for line in stdout.splitlines():
            cols = line.split()
            if not cols:
                continue
            jid = cols[0].strip()
            if not jid or not jid[0].isdigit():
                continue  # header / separator line
            if jid in wanted:
                alive.add(jid)
        return alive

    @staticmethod
    def stderr_log_path(remote_path: str, job_name: str, job_id: str, task_id: int) -> str:
        """Cluster-side stderr path for SGE.

        ``_build_command`` passes ``-o <log_dir>`` to ``qsub`` and the
        runtime array templates default ``log_dir`` to ``logs`` (relative
        to the run dir, which is ``remote_path``); with ``-j y`` the
        ``.o`` (output) file holds both streams. SGE names the file
        ``<job_name>.o<job_id>.<SGE_TASK_ID>`` with a 1-based
        ``SGE_TASK_ID``, while the array scripts derive the logical
        0-based ``task_id`` as ``SGE_TASK_ID - 1`` (offset 0) — so the
        on-disk filename index is ``task_id + 1``.
        """
        return f"{remote_path.rstrip('/')}/logs/{job_name}.o{job_id}.{task_id + 1}"

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
        from hpc_agent.infra.backends.query import query_sge

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
        from hpc_agent.infra.inspect.sge import _sge_inspect

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
        # SGE's ``qsub -v`` uses comma to separate K=V pairs, so a value
        # containing a comma silently splits into extra malformed pairs
        # on the scheduler side — e.g. ``MODULES="python/3.11,gcc/11"``
        # corrupts the cluster-side env. Reject up front. SLURM has the
        # symmetric guard in ``infra.backends.slurm``.
        bad = [k for k, v in job_env.items() if k in self.pass_env_keys and "," in str(v)]
        if bad:
            raise errors.SpecInvalid(
                "SGE qsub -v cannot transport env values containing "
                f"','; offending keys: {sorted(bad)}. Pre-encode "
                "(base64, space-delimited list, etc.) before submission."
            )
        pass_vars = ",".join(f"{k}={v}" for k, v in job_env.items() if k in self.pass_env_keys)
        if pass_vars:
            cmd += ["-v", pass_vars]
        if extra_flags:
            cmd += extra_flags
        cmd.append(self.script)
        return cmd
