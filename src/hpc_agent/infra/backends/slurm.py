"""SLURM backend — submits array jobs via sbatch.

The wire-facing ``backend`` value ``"slurm"`` resolves to
:class:`hpc_agent.infra.backends.slurm_remote.RemoteSlurmBackend` (the
remote-over-ssh subclass). This local class is no longer registered
because nothing in src/ or tests/ submits jobs from a local SLURM shell —
every submission flows through the SSH boundary. It remains as a base
class for the remote subclass.
"""

import os
import re

from hpc_agent import errors
from hpc_agent.infra.backends import HPCBackend


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
            raise errors.SpecInvalid("SlurmBackend requires a 'script' path")
        self.script = script
        self.account = account or os.environ.get("SLURM_ACCOUNT", "")
        self.cluster = cluster or os.environ.get("SLURM_CLUSTER", "")
        self.log_dir = log_dir or os.environ.get("SLURM_LOG_DIR", "logs")

    def _build_dependency_flag(self, job_ids: list[str]) -> list[str]:
        if not job_ids:
            return []
        return ["--dependency", f"afterany:{':'.join(job_ids)}"]

    def resource_flags(self, resources: object) -> list[str]:
        """Emit SLURM ``--time`` / ``--mem`` / ``--cpus-per-task`` for set asks.

        Opt-in per field — an empty ``resources`` emits nothing, so the
        template's ``#SBATCH`` directives (e.g. the hardcoded 6h walltime)
        still apply. ``--time`` is given in whole minutes (rounded up so a
        sub-minute ask never truncates to 0).
        """
        flags: list[str] = []
        if resources is None:
            return flags
        walltime_sec = getattr(resources, "walltime_sec", None)
        mem_mb = getattr(resources, "mem_mb", None)
        cpus = getattr(resources, "cpus", None)
        if walltime_sec:
            minutes = -(-int(walltime_sec) // 60)  # ceil division
            flags += ["--time", str(minutes)]
        if mem_mb:
            flags += ["--mem", f"{int(mem_mb)}M"]
        if cpus:
            flags += ["--cpus-per-task", str(int(cpus))]
        return flags

    # ------------------------------------------------------------------
    # B5-PR2 capability hooks — pure, scheduler-shape-only helpers.
    # Callers (runner.py, status.py) pair these with their own SSH /
    # subprocess execution so the backend stays transport-agnostic.
    # ------------------------------------------------------------------

    @staticmethod
    def build_alive_check_cmd(job_ids: list[str]) -> str:
        """Shell command whose stdout lists the live SLURM job ids.

        Uses ``squeue`` (active states only) so completed/failed jobs do
        NOT show up — keeping this aligned with sacct would leak history
        and make abandoned-run detection useless.
        """
        import shlex

        if not job_ids:
            return "true"
        csv = ",".join(job_ids)
        return f"squeue -j {shlex.quote(csv)} -h -o '%i' 2>/dev/null || true"

    @staticmethod
    def parse_alive_output(stdout: str, job_ids: list[str]) -> set[str]:
        """Filter ``squeue`` output to the requested *job_ids*."""
        alive: set[str] = set()
        wanted = set(job_ids)
        for line in stdout.splitlines():
            token = line.strip()
            if not token:
                continue
            base = token.split(".")[0].split("_")[0]
            if base in wanted:
                alive.add(base)
        return alive

    @staticmethod
    def build_scheduler_state_cmd(job_ids: list[str]) -> str:
        """Shell command whose stdout pairs each live job id with its state.

        ``squeue -o '%i %T'`` adds the state column the alive-check omits;
        ``|| true`` keeps rc 0 when squeue knows none of the ids (all gone).
        A job stuck pending/held still appears here with its state.
        """
        import shlex

        if not job_ids:
            return "true"
        csv = ",".join(job_ids)
        return f"squeue -j {shlex.quote(csv)} -h -o '%i %T' 2>/dev/null || true"

    @staticmethod
    def parse_scheduler_states(stdout: str, job_ids: list[str]) -> dict[str, str]:
        """Map each requested job id present in ``squeue`` output to its raw
        state token (``%T``), e.g. ``RUNNING``, ``PENDING``, ``FAILED``.
        """
        states: dict[str, str] = {}
        wanted = set(job_ids)
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            base = parts[0].split(".")[0].split("_")[0]
            if base in wanted:
                states[base] = parts[1].strip()
        return states

    @staticmethod
    def classify_scheduler_state(state: str) -> str:
        """Bucket a raw SLURM state token into ``alive`` / ``error`` / ``held``.

        Active healthy states (``RUNNING``, ``PENDING``, ``COMPLETING`` …) are
        alive; the failed/cancelled family is an error; an explicitly held job
        (``*_HOLD``) is held. Unknown tokens are treated as alive (conservative
        — the verb still surfaces the raw state for the agent to judge).
        """
        s = state.strip().upper()
        if s in {
            "FAILED",
            "NODE_FAIL",
            "BOOT_FAIL",
            "DEADLINE",
            "OUT_OF_MEMORY",
            "CANCELLED",
            "TIMEOUT",
            "PREEMPTED",
            "REVOKED",
        }:
            return "error"
        if "HOLD" in s or s == "SPECIAL_EXIT":
            return "held"
        return "alive"

    @staticmethod
    def stderr_log_path(remote_path: str, job_name: str, job_id: str, task_id: int) -> str:
        """Cluster-side path to a single task's stderr log.

        ``_build_command`` passes ``--error <log_dir>/%x_%A_%a.err`` to
        ``sbatch`` and the runtime array templates default ``log_dir`` to
        ``logs`` (relative to the run dir, which is ``remote_path``).
        SLURM expands ``%x``->job-name, ``%A``->array-master job id,
        ``%a``->the 1-based array index. The array scripts derive the
        logical 0-based ``task_id`` as ``%a - 1`` (offset 0), so the
        on-disk filename index is ``task_id + 1``.
        """
        return f"{remote_path.rstrip('/')}/logs/{job_name}_{job_id}_{task_id + 1}.err"

    @staticmethod
    def err_log_disk_path(
        log_dir: str, scratch_dir: str, job_name: str, job_id: str, task_id: int
    ) -> str:
        """Local-disk path used by ``status.get_err_log_paths`` for SLURM."""
        import os

        return os.path.join(log_dir, f"{job_name}_{job_id}_{task_id}.err")

    @staticmethod
    def query_jobs(
        job_ids: list[str],
        *,
        sge_user: str | None = None,
        slurm_cluster: str | None = None,
    ) -> dict:
        """Dispatch to ``query_sacct`` for SLURM job state.

        Unified signature across SGE and SLURM so reduce.status can call
        ``backend_cls.query_jobs(...)`` without an inline ladder. The
        unused kwarg is ignored (sge_user is irrelevant for SLURM).
        """
        from hpc_agent.infra.backends.query import query_sacct

        return query_sacct(job_ids, cluster=slurm_cluster)

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
        """Dispatch to :func:`_slurm_inspect` for SLURM.

        Unified signature with :meth:`SGEBackend.inspect_cluster`; SLURM
        consumes ``sacct_window_hours`` (used to scope the failure-rate
        sacct query) while SGE ignores it.
        """
        from hpc_agent.infra.inspect.slurm import _slurm_inspect

        return _slurm_inspect(
            cluster_name,
            cfg,
            sacct_window_hours=sacct_window_hours,
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
            # SLURM's --export uses comma to separate K=V pairs, so a value
            # containing a comma silently splits into extra malformed pairs
            # on the scheduler side — e.g. ``MODULES="python/3.11,gcc/11"``
            # corrupts the cluster-side env. Reject up front rather than
            # silently truncate (matches v2's SGE-side guard in
            # ``infra.backends.sge`` — v3 BUG-6V3-2).
            bad = [k for k, v in job_env.items() if "," in str(v)]
            if bad:
                raise errors.SpecInvalid(
                    "SLURM --export cannot transport env values containing "
                    f"','; offending keys: {sorted(bad)}. Pre-encode "
                    "(base64, space-delimited list, etc.) before submission."
                )
            export_str = ",".join(f"{k}={v}" for k, v in job_env.items())
            cmd += ["--export", f"ALL,{export_str}"]
        if extra_flags:
            cmd += extra_flags
        cmd.append(self.script)
        return cmd
