"""
Pluggable HPC backend system.

Provides an abstract interface for job submission so any project
can target any scheduler (SLURM, SGE, PBS, ...) without changing
the core submission logic.

Usage:
    from hpc_agent.infra.backends import get_backend
    backend = get_backend("slurm", script="path/to/job.slurm")
    backend.submit_plan(plan, job_name, job_env)
"""

from __future__ import annotations

__all__ = [
    "BackendBuildContext",
    "HPCBackend",
    "ProfileBackend",
    "RemoteProfileBackend",
    "backend_requires_ssh",
    "build_backend_class",
    "get_backend",
    "get_backend_class",
    "register",
    "register_profile",
    "registered_backend_names",
    "template_ext_for",
]

import abc
import importlib
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors

if TYPE_CHECKING:
    from hpc_agent.infra.throughput import JobBatch, SubmissionPlan


# Default fallback regex for parsing job IDs out of scheduler stdout.
# Backends override ``JOB_ID_REGEX`` with a scheduler-specific anchor so
# that warnings or banners containing digits (``sbatch: warning: 30%
# pre-empt; Submitted batch job 12345``) don't poison the parse with a
# stray ``30``.
_DEFAULT_JOB_ID_REGEX = re.compile(r"(\d+)")

# Env var carrying a wave batch's GLOBAL 0-based offset to the cluster job.
# On an index-bounded backend ``submit_plan`` submits each batch as a LOCAL
# array ``1-<size>`` and injects this per batch; the runtime templates
# (``_scripts.py``) recover the global task id as
# ``<scheduler array index> - 1 + ${TASK_OFFSET:-0}``. Injected only when the
# offset is non-zero, so a ≤cap single wave-0 array stays byte-identical. Kept
# as a module constant so the submit edge and the template arithmetic name the
# same variable.
_TASK_OFFSET_ENV = "TASK_OFFSET"

# Default subprocess timeout for ``qsub``/``sbatch`` invocations.  A
# hung scheduler binary (NFS stall, scheduler outage) would otherwise
# block the agent indefinitely; we surface ``TimeoutExpired`` so callers
# can map it to a cluster-category error.
SUBMIT_TIMEOUT_SEC = 120


@dataclass(frozen=True)
class BackendBuildContext:
    """Everything the submit/recover flows know when constructing a backend.

    The construction seam for plugin-registered backends
    (``docs/proposals/crowd-compute-backend.md``):
    ``remote_factory.build_remote_backend``'s inline ladder knows the
    SSH-shaped constructor kwargs of the built-in families; any other
    registered backend receives this whole context via
    :meth:`HPCBackend.from_build_context` and decides for itself which
    fields it needs. The SSH-shaped fields are populated but a backend
    is free to ignore them — a pure-API (crowd-compute) backend reads
    its own configuration from the environment instead.
    """

    backend_name: str
    script: str
    ssh_target: str
    remote_path: str
    pass_env_keys: tuple[str, ...] | None
    job_env_keys: tuple[str, ...]
    slurm_account: str | None = None
    slurm_cluster: str | None = None
    # Bound transport: ``(cmd) -> CompletedProcess`` against ssh_target.
    # An SSH-shaped plugin backend (e.g. a marketplace renting SSH-able
    # instances) can reuse it; API-driven backends ignore it.
    ssh_run: Callable[[str], subprocess.CompletedProcess[str]] | None = None


class HPCBackend(abc.ABC):
    """Minimal interface for HPC job submission backends.

    Subclasses implement ``_build_command`` to construct the scheduler-specific
    command.  Override ``_execute_command`` to change how commands are run
    (e.g. via SSH) and ``_setup_log_dir`` for remote ``mkdir``.

    B5: widened with class-level metadata that the rest of the
    framework historically obtained via ``if scheduler == "slurm"``
    branches sprinkled across 16 callsites. New callers should consult
    these attributes / methods rather than re-parsing the scheduler
    name.
    """

    # Scheduler name — subclasses set to "slurm" / "sge" / etc. Allows
    # ``isinstance`` lookups to be replaced with attribute reads in the
    # planner / status code that needs to dispatch on scheduler kind.
    scheduler_name: str = ""

    # Template-script extension. The framework currently has 3 hard-
    # coded ``if scheduler == "slurm" else "sge"`` blocks that compute
    # ``.slurm`` vs ``.sge``; subclasses publish their canonical
    # extension here so callers can do ``backend.template_ext``.
    template_ext: str = ""

    # Whether the backend supports ``sbatch --test-only``-style ETA
    # probes used by the backfill planner. SLURM does, SGE does not;
    # the planner currently checks via ``if scheduler == "slurm"``.
    supports_test_only_eta: bool = False

    # Whether this backend reaches its scheduler over SSH to a login node with
    # a shared filesystem (the built-in SGE/SLURM/PBS families) — as opposed to
    # a pure-API "crowd-compute" backend that dispatches over HTTPS and ships
    # data/results as artifacts (docs/proposals/crowd-compute-backend.md). The
    # submit prelude, preflight, monitor, and aggregate flows read this to skip
    # their SSH / shared-filesystem steps for a pure-API backend instead of
    # re-deriving it from the scheduler name. Default True (the SSH ladder); a
    # pure-API backend overrides to False and implements ``fetch_results`` /
    # ``fetch_logs`` (below) as the artifact-based replacement for the rsync pull.
    requires_ssh: bool = True

    # Hard *platform* ceiling on the task count this backend can submit in a
    # single scheduler array, independent of any clusters.yaml config. ``None``
    # (the default for the built-in SSH families) means the backend imposes no
    # cap of its own — the effective ceiling is then the cluster's
    # ``constraints.max_array_size`` if one is declared. A pure-API backend
    # whose platform caps the array overrides this (GitHub Actions = 256 matrix
    # cells/run) so submit-flow can reject an over-cap sweep with a clean
    # ``SpecInvalid`` *before* dispatch, instead of a low-signal platform error
    # after (a >256-cell matrix that GitHub Actions rejects post-dispatch).
    # Read off the *class* so the guard never pays the constructor cost.
    max_array_size: int | None = None

    # Whether this backend can split an over-cap sweep into multiple
    # concurrency-bounded WAVES (#339). The shared wave submitter
    # (:meth:`submit_plan`) drives every batch through the same per-batch
    # primitive (:meth:`submit_one`), so any backend that can submit one array
    # can submit N waves of arrays — hence the default ``True``. A backend that
    # genuinely cannot wave (e.g. a one-shot dispatch with no per-wave
    # sequencing) overrides to ``False``, and the submit-flow array-cap guard
    # then hard-rejects an over-cap sweep instead of routing it through waves.
    # Read off the *class* (a capability, not per-run state) so the guard pays
    # no constructor cost.
    can_wave: bool = True

    # Whether the scheduler treats an array job's index space as GLOBAL and
    # arbitrary (a wave can submit ``--array=1001-2000`` directly) — as opposed
    # to BOUNDING the array index (SLURM ``MaxArraySize``, the SGE/PBS
    # analogues), where a valid index is ``1 .. cap``. Waving fires exactly when
    # ``total_tasks > max_array_size``, so on an index-bounded backend every wave
    # past wave 0 would otherwise emit an array range ABOVE the cap and the
    # scheduler rejects the whole batch (``Invalid job array specification``) —
    # the post-dispatch failure waves exist to prevent. :meth:`submit_plan`
    # reads this:
    #   * True  → submit each batch's GLOBAL ``task_range`` unchanged (GitHub
    #     Actions: matrix cell values are arbitrary, and its ``_execute_command``
    #     already builds a global window).
    #   * False → submit a LOCAL ``1-<array_size>`` array per batch + a per-batch
    #     ``TASK_OFFSET`` so the job recovers its global 0-based id (the
    #     ``_scripts.py`` templates do ``<index> - 1 + ${TASK_OFFSET:-0}``); the
    #     offset is 0 for a single wave-0 array, keeping a ≤cap sweep
    #     byte-for-byte unchanged.
    # Default False = "the scheduler bounds the array index" (slurm / sge /
    # pbspro / torque). A pure-API backend that builds its own global window
    # overrides to True.
    uses_global_array_index: bool = False

    log_dir: str  # subclasses must set this
    JOB_ID_REGEX: re.Pattern[str] = _DEFAULT_JOB_ID_REGEX

    # ------------------------------------------------------------------
    # Capability hooks — additive in B5-PR1. Subclasses override; the
    # default raises NotImplementedError so callers that haven't
    # migrated yet still see a clear failure mode rather than a silent
    # wrong answer.
    # ------------------------------------------------------------------

    def alive_job_ids(self, job_ids: list[str]) -> list[str]:
        """Return the subset of *job_ids* still known to the scheduler.

        Used by the slash-command runner to detect abandoned runs and
        by reduce.status to short-circuit polling once every job has
        terminated. Default raises so an unmigrated backend is loud.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement alive_job_ids")

    def task_statuses(self, job_ids: list[str], *, total_tasks: int) -> dict[int, str]:
        """Per-task status map (0-based task id → ``TaskStatus`` value) for a run.

        The richer-status counterpart of :meth:`alive_job_ids`: a pure-API
        backend that can report per-task progress over its API (e.g. per-task
        result artifacts present vs. the run still in flight) overrides this so
        the monitor reports real complete / running / pending / failed counts
        instead of run-level liveness alone. Keys are 0-based task ids in
        ``range(total_tasks)``; values are
        ``hpc_agent._kernel.contract.vocabulary.TaskStatus`` members. Default
        raises so a backend that only knows liveness falls back to the liveness
        summary, matching the other capability-hook defaults.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement task_statuses")

    def inspect(self, cluster_name: str, **kwargs: Any) -> Any:
        """Return a :class:`ClusterSnapshot` for *cluster_name*.

        Wraps :func:`hpc_agent.infra.inspect.inspect_cluster`'s
        existing per-scheduler dispatch. Subclasses override; the
        default raises so an unmigrated backend is loud.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement inspect")

    def fetch_results(self, run_id: str, dest_dir: str) -> list[str]:
        """Download a run's per-task artifacts into *dest_dir*; return the dirs.

        The shared-filesystem replacement for a pure-API backend
        (``requires_ssh = False``): instead of the aggregate flow rsync-pulling
        result dirs off a login node, the backend pulls the run's per-task
        artifacts over its API into ``task-<i>`` dirs under *dest_dir*. It must
        bring back the FULL per-task outputs — not just ``metrics.json`` — so
        aggregate-flow can run either reducer locally: the weighted-mean over
        each task's ``metrics.json`` (``combiner-only``) OR a caller-owned
        ``aggregate_cmd`` over the raw artifacts (``cluster-reduce`` mode, e.g.
        CSVs/parquet a mean can't reduce). SSH backends never reach this hook —
        their results come back over rsync — so the default raises, matching the
        other capability hooks.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement fetch_results")

    def fetch_logs(self, run_id: str, dest_dir: str | None = None) -> str:
        """Download a run's task logs into *dest_dir*; return the written path.

        The pure-API counterpart of :meth:`stderr_log_path` followed by an ssh
        ``tail``: a ``requires_ssh = False`` backend overrides this to pull its
        logs over the API. Default raises, matching the other capability hooks.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement fetch_logs")

    # ------------------------------------------------------------------
    # B5-PR2 capability hooks — staticmethods so callers can invoke them
    # off the *class* (cheap path that skips constructor kwargs).
    # Subclasses override; defaults raise NotImplementedError so an
    # unmigrated backend is loud.
    # ------------------------------------------------------------------

    @staticmethod
    def build_alive_check_cmd(job_ids: list[str]) -> str:
        """Shell command whose stdout lists the live job ids."""
        raise NotImplementedError("backend does not implement build_alive_check_cmd")

    @staticmethod
    def parse_alive_output(stdout: str, job_ids: list[str]) -> set[str]:
        """Parse ``build_alive_check_cmd`` stdout to the live subset of *job_ids*."""
        raise NotImplementedError("backend does not implement parse_alive_output")

    @staticmethod
    def build_scheduler_state_cmd(job_ids: list[str]) -> str:
        """Shell command whose stdout pairs each job id with its raw scheduler
        state (consumed by :meth:`parse_scheduler_states`).

        Distinct from :meth:`build_alive_check_cmd`: the alive-check answers
        only "still in the queue?", whereas this surfaces the *state* so a
        post-submit check can tell a healthy ``running``/``pending`` job from
        an error/held one (SGE ``Eqw``, a held SLURM job) — the question
        ``verify-submitted`` answers.
        """
        raise NotImplementedError("backend does not implement build_scheduler_state_cmd")

    @staticmethod
    def parse_scheduler_states(stdout: str, job_ids: list[str]) -> dict[str, str]:
        """Map each requested job id present in *stdout* to its raw scheduler
        state token (SGE ``Eqw`` / SLURM ``FAILED`` …). Ids absent from the
        output are omitted — the caller treats a missing id as gone."""
        raise NotImplementedError("backend does not implement parse_scheduler_states")

    @staticmethod
    def classify_scheduler_state(state: str) -> str:
        """Bucket a raw scheduler state token into ``alive`` / ``error`` / ``held``."""
        raise NotImplementedError("backend does not implement classify_scheduler_state")

    @staticmethod
    def batch_status(states: dict[str, str]) -> dict[str, str]:
        """Map raw scheduler state tokens to ``TaskStatus`` values, in bulk.

        *states* is ``{job_id: raw_state_token}`` — exactly the shape
        :meth:`parse_scheduler_states` returns from ONE ``qstat -u $USER`` /
        ``squeue`` query, so the whole batch is classified without a second
        scheduler round-trip per run. Returns ``{job_id: TaskStatus.value}``.

        Finer-grained than :meth:`classify_scheduler_state`'s 3-bucket
        alive/error/held: it separates a queued job (``PENDING``) from a
        running one (``RUNNING``) so the monitor's per-tick diff reads real
        progress. A job *absent* from *states* has left the queue and is the
        caller's terminal-vs-complete decision — it is simply omitted here.
        """
        raise NotImplementedError("backend does not implement batch_status")

    @staticmethod
    def stderr_log_path(remote_path: str, job_name: str, job_id: str, task_id: int) -> str:
        """Return the cluster-side path to a single task's stderr log.

        Used by /failures and the auto-retry resolver to fetch
        per-task stderr without re-deriving the path from the
        scheduler-specific %x_%A_%a / job-array format string.
        """
        raise NotImplementedError("backend does not implement stderr_log_path")

    @staticmethod
    def err_log_disk_path(
        log_dir: str, scratch_dir: str, job_name: str, job_id: str, task_id: int
    ) -> str:
        """Local-disk path used by ``status.get_err_log_paths``."""
        raise NotImplementedError("backend does not implement err_log_disk_path")

    @staticmethod
    def query_jobs(
        job_ids: list[str],
        *,
        sge_user: str | None = None,
        slurm_cluster: str | None = None,
    ) -> dict[str, Any]:
        """Return per-job state map for *job_ids* via the scheduler's history."""
        raise NotImplementedError("backend does not implement query_jobs")

    @classmethod
    def render_script(cls, *, kind: str, **_opts: Any) -> str:
        """Return the runtime array-job script body for *kind* (cpu/gpu).

        Profile-driven backends render it from ``cls.profile``; the base
        raises so an unmigrated backend is loud rather than silently wrong.
        """
        raise NotImplementedError("backend does not implement render_script")

    @staticmethod
    def inspect_cluster(
        cluster_name: str,
        cfg: dict[str, Any],
        *,
        sacct_window_hours: int = 24,
        stress_alloc_mem_pct: float = 0.80,
        stress_cpu_load_frac: float = 0.80,
        runner: Any = None,
    ) -> Any:
        """Return a ``ClusterSnapshot`` for *cluster_name* (B5-PR2)."""
        raise NotImplementedError("backend does not implement inspect_cluster")

    @classmethod
    def from_build_context(cls, ctx: BackendBuildContext) -> HPCBackend:
        """Construct this backend from the submit-flow build context.

        The construction seam for plugin-registered backends
        (``docs/proposals/crowd-compute-backend.md``):
        ``remote_factory.build_remote_backend`` constructs the built-in
        families through its inline ladder (their SSH-shaped kwargs are
        its business); any *other* registered backend is handed the
        whole :class:`BackendBuildContext` here and owns the decision of
        which fields matter — a crowd-compute backend typically ignores
        the SSH pair and reads its API key / image from the environment.
        Default raises so a plugin backend that hasn't opted into flow
        construction fails loud at submit time, matching the other
        capability-hook defaults.
        """
        raise NotImplementedError(f"{cls.__name__} does not implement from_build_context")

    @abc.abstractmethod
    def _build_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        """Return the scheduler command for the given task range.

        *array* selects the array shape (``task_range`` elements); a single
        multi-rank MPI job (#293) passes ``array=False`` with ``task_range=None``.
        """
        ...

    def resource_flags(self, resources: Any) -> list[str]:
        """Translate a resources object into scheduler command-line flags.

        *resources* is a ``SubmitResources`` (or ``None``). The base
        returns ``[]`` so a backend that hasn't opted in — and any call
        with no resources set — emits no new flags, leaving the job
        template directives and cluster defaults untouched. SGE / SLURM
        override this. A command-line flag overrides the matching
        ``#$``/``#SBATCH`` directive baked into the template, which is the
        only way to vary a per-submission resource (SGE ``#$`` directives
        cannot read env vars).
        """
        return []

    def _execute_command(
        self,
        cmd: list[str],
        job_env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a scheduler command.  Override for remote execution."""
        return subprocess.run(
            cmd,
            env=job_env,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=SUBMIT_TIMEOUT_SEC,
        )

    def _setup_log_dir(self) -> None:
        """Ensure the log directory exists.  Override for remote ``mkdir``."""
        os.makedirs(self.log_dir, exist_ok=True)

    def _build_dependency_flag(self, job_ids: list[str]) -> list[str]:
        """Return scheduler flags to depend on the given job IDs.

        Override in subclasses for scheduler-specific syntax.
        """
        return []

    @property
    def supports_afterok(self) -> bool:
        """Whether this backend expresses an afterok (success-only) dependency (#250).

        Default ``False`` (no dependency support); the profile-driven engine
        overrides for SLURM / PBS families.
        """
        return False

    def _build_afterok_dependency_flag(self, job_ids: list[str]) -> list[str]:
        """Scheduler flags making this job depend on *job_ids* SUCCEEDING (#250).

        Default ``[]`` (unsupported); override per scheduler. See
        :meth:`ProfileBackend._build_afterok_dependency_flag`.
        """
        return []

    def _build_wave_dependency_flag(
        self, *, afterok_ids: list[str], afterany_ids: list[str]
    ) -> list[str]:
        """One combined dependency flag: success-gate AND/OR completion-gate (#339).

        Used by :meth:`submit_plan` so each wave can depend on the canary
        SUCCEEDING (``afterok_ids``) and on the prior wave COMPLETING
        (``afterany_ids``) in a single scheduler flag. Default ``[]``
        (no dependency support); the profile-driven engine overrides it. See
        :meth:`ProfileBackend._build_wave_dependency_flag`.
        """
        return []

    def submit_one(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        cwd: Path | None = None,
        array: bool = True,
        setup_log_dir: bool = True,
    ) -> str:
        """Submit ONE scheduler array (or one non-array job) and return its id.

        The single per-batch submission primitive shared by every submit edge
        (#339 increment 3): :meth:`submit_plan`'s wave loop, submit-flow's
        ``_make_single_array_submission`` (the ≤cap fast path and the canary),
        and recover-flow's ``_submit_one_batch`` all collapse onto this one
        ``setup_log_dir + _build_command + _execute_command + returncode-check +
        JOB_ID_REGEX`` sequence so the qsub edge lives in exactly one place.

        *task_range* is the scheduler array expression (``"1-100"``,
        ``"4,8,13-15"``), or ``None`` together with ``array=False`` for a single
        multi-rank MPI job (#293). *extra_flags* are appended verbatim to the
        built command (resource flags, an afterok/hold dependency, planner
        overrides). *setup_log_dir* lets a caller that has already ensured the
        log directory (``submit_plan`` does it once per plan) skip the redundant
        per-batch ``mkdir``.

        Raises
        ------
        RuntimeError
            If the submission exits non-zero (message carries the command and
            stderr) or the scheduler stdout has no parseable job id.
        """
        cwd = cwd or Path.cwd()
        if setup_log_dir:
            self._setup_log_dir()
        cmd = self._build_command(
            task_range, job_name, job_env, extra_flags=extra_flags, array=array
        )
        result = self._execute_command(cmd, job_env, cwd)
        if result.returncode != 0:
            stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
            raise RuntimeError(
                f"Job submission failed (exit {result.returncode}) "
                f"for array {task_range}:\n"
                f"  command: {' '.join(cmd)}\n"
                f"  stderr:  {stderr_msg}"
            )
        match = self.JOB_ID_REGEX.search(result.stdout)
        if not match:
            raise RuntimeError(f"Could not parse job ID from output: {result.stdout!r}")
        return match.group(1)

    def submit_plan(
        self,
        plan: SubmissionPlan,
        job_name: str,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
        per_wave_extra_flags: list[str] | None = None,
        gate_job_ids: list[str] | None = None,
    ) -> list[tuple[int, str, str]]:
        """Submit a :class:`SubmissionPlan` as wave-sequenced array jobs (#339).

        The shared, subject-neutral wave submitter for both ``ops/submit`` and
        ``ops/recover`` (it lives on :class:`HPCBackend` so neither subject
        imports the other). Each :class:`JobBatch` is submitted as one global
        sub-array using its :attr:`JobBatch.task_range`, routed through the
        shared per-batch primitive :meth:`submit_one` so the qsub edge
        (``_build_command`` / ``_execute_command`` / ``JOB_ID_REGEX``) lives in
        exactly one place.

        Batches are grouped by :attr:`JobBatch.wave` and waves are submitted in
        ascending order. When ``max_concurrent_jobs`` > 1 yields multiple waves,
        each wave after wave 0 is *chained* behind the prior wave for
        concurrency bounding via a **completion** dependency (``afterany``):
        wave N starts once wave N-1 has TERMINATED, regardless of per-task
        outcome. Completion (not success) is deliberate — the waves are
        independent slices of one sweep, so a single failed task must NOT cancel
        the later, unrelated waves (a success-gate would, losing work). The
        success-gate is reserved for the canary below.

        *gate_job_ids* are job ids that EVERY wave must depend on SUCCEEDING
        (the canary, #339 increment 4). Because the inter-wave dependency is
        completion-only it does not propagate a canary failure on its own, so
        the gate is applied to every wave directly (not just wave 0). The
        per-wave success-gate and the inter-wave completion-gate are ANDed into
        a single scheduler dependency flag by
        :meth:`_build_wave_dependency_flag`. *per_wave_extra_flags* (resource
        flags) are applied to every wave.

        Returns ``(wave, task_range, job_id)`` tuples in submission order.

        Raises
        ------
        RuntimeError
            If a submission exits non-zero (message carries the command and
            stderr) or the scheduler stdout has no parseable job id.
        """
        cwd = cwd or Path.cwd()
        # Ensure the log dir once for the whole plan; each per-batch
        # ``submit_one`` below skips its own (idempotent) ``mkdir``.
        self._setup_log_dir()

        # The per-batch offset ships to the job as ``TASK_OFFSET`` (read by the
        # cluster templates). The scheduler env builders transport it as a
        # framework-internal var whenever present — SGE/PBS ``-v`` special-case
        # it past the ``pass_env_keys`` allowlist, SLURM ``--export ALL`` already
        # carries everything — so no per-call backend mutation is needed here.

        # Group batches by wave.
        waves: dict[int, list[JobBatch]] = defaultdict(list)
        for batch in plan.batches:
            waves[batch.wave].append(batch)

        submissions: list[tuple[int, str, str]] = []
        prev_wave_ids: list[str] = []
        gate_ids = list(gate_job_ids or [])

        for wave_num in sorted(waves):
            # Every wave success-gates on the canary (gate_ids); waves after the
            # first ALSO complete-gate on the prior wave for concurrency bounding
            # (afterany — must not drop later waves on a partial failure). Both
            # conditions are merged into one dependency flag (a scheduler accepts
            # only one). Resource flags apply to every wave.
            afterany_ids = prev_wave_ids if wave_num > 0 else []
            dep_flags = list(per_wave_extra_flags or []) + self._build_wave_dependency_flag(
                afterok_ids=gate_ids, afterany_ids=afterany_ids
            )

            current_wave_ids: list[str] = []
            for batch in waves[wave_num]:
                # Render the batch per the index-space capability:
                #   * global-index backend (GHA) → its GLOBAL window
                #     (``batch.task_range``) + the shared env, unchanged.
                #   * index-bounded backend → a LOCAL ``1-<array_size>`` array
                #     (always within the cap) + a per-batch ``TASK_OFFSET`` so the
                #     job recovers its global id. The offset is injected ONLY when
                #     ``task_start > 1``, so a single wave-0 array ``1-N`` (offset
                #     0) emits the SAME command AND env as a pre-wave ≤cap sweep.
                if self.uses_global_array_index:
                    task_range = batch.task_range
                    batch_env = job_env
                else:
                    task_range = f"1-{batch.array_size}"
                    offset = batch.task_start - 1
                    batch_env = (
                        {**job_env, _TASK_OFFSET_ENV: str(offset)} if offset > 0 else job_env
                    )
                try:
                    job_id = self.submit_one(
                        task_range,
                        job_name,
                        batch_env,
                        extra_flags=dep_flags,
                        cwd=cwd,
                        setup_log_dir=False,
                    )
                except RuntimeError as exc:
                    # Partial accounting on mid-plan failure (#339 inc 4),
                    # mirroring _submit_flow_batch_locked's partial_submit_results:
                    # the ids that DID land (every prior wave + this wave's
                    # earlier batches) are attached to the raised exception so the
                    # caller can pre-stamp / reconcile them instead of losing them
                    # with a bare raise. The chained-dependency waves that never
                    # fired are simply absent — the scheduler drops them anyway.
                    exc.partial_submit_results = list(submissions)  # type: ignore[attr-defined]
                    exc.failed_wave = wave_num  # type: ignore[attr-defined]
                    raise
                current_wave_ids.append(job_id)
                submissions.append((wave_num, batch.task_range, job_id))

            prev_wave_ids = current_wave_ids

        return submissions


_REGISTRY: dict[str, type[HPCBackend]] = {}

# Plugin modules whose import already failed once. ``registered_backend_names``
# runs on every wire validation of a backend name; without this, a single broken
# plugin would re-emit the same import warning on every spec validated this
# process. Warn once per module instead.
_WARNED_BROKEN_PLUGIN_MODULES: set[str] = set()


def register(name: str) -> Callable[[type[HPCBackend]], type[HPCBackend]]:
    """Decorator to register a backend class."""

    def decorator(cls: type[HPCBackend]) -> type[HPCBackend]:
        _REGISTRY[name] = cls
        return cls

    return decorator


def _populate_registry() -> None:
    """Import every backend module so its ``@register`` decorator fires.

    Both ``get_backend`` and ``get_backend_class`` populate the registry on
    every call (the modules are cached in ``sys.modules`` after the first
    import, so this is cheap). The two helpers previously had divergent
    import lists, and the backend rename to ``{sge, slurm}`` (which moved
    the ``@register`` decorators onto the remote subclasses) silently broke
    ``get_backend("slurm")`` when ``slurm_remote`` was missing here.
    Sharing one populator removes that footgun.
    """
    from hpc_agent.infra.backends import sge_remote as _sge_remote  # noqa: F401
    from hpc_agent.infra.backends import slurm_remote as _slurm_remote  # noqa: F401

    # PBS family (pbspro / torque) ships as golden profiles rather than
    # hand-written remote classes — register them directly (not via
    # register_profile, which would recurse into this populator).
    from hpc_agent.infra.backends.profile import PBSPRO_PROFILE, TORQUE_PROFILE

    for _prof in (PBSPRO_PROFILE, TORQUE_PROFILE):
        if _prof.name not in _REGISTRY:
            _REGISTRY[_prof.name] = build_backend_class(_prof, remote=True)


def get_backend(name: str = "slurm", **kwargs: object) -> HPCBackend:
    """Instantiate a backend by name.  *kwargs* are forwarded to the constructor."""
    _populate_registry()
    if name not in _REGISTRY:
        raise errors.SpecInvalid(f"Unknown backend {name!r}. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def get_backend_class(name: str) -> type[HPCBackend]:
    """Return the backend *class* (not an instance) by scheduler name.

    Useful when you need a class-level attribute (``template_ext``,
    ``scheduler_name``, ``supports_test_only_eta``) or a ``@staticmethod``
    helper (``build_alive_check_cmd``, ``stderr_log_path``, ...) without
    paying the constructor's required-kwarg cost. Migrating callers
    away from inline ``if scheduler == "slurm"`` ladders should use
    this when the script-path / SSH-target context is not available.
    """
    _populate_registry()
    if name not in _REGISTRY:
        raise errors.SpecInvalid(f"Unknown backend {name!r}. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def template_ext_for(scheduler: str) -> str:
    """Convenience accessor for ``get_backend_class(scheduler).template_ext``."""
    return get_backend_class(scheduler).template_ext


def backend_requires_ssh(name: str) -> bool:
    """Whether the backend named *name* reaches its scheduler over SSH.

    Reads the class-level :attr:`HPCBackend.requires_ssh` capability WITHOUT
    constructing the backend — the submit prelude / preflight branch on this
    before any backend instance exists. Built-in families default to ``True``;
    a pure-API plugin backend returns ``False``.

    Goes through :func:`registered_backend_names` (not bare
    :func:`get_backend_class`) so a plugin module is imported for its
    ``@register`` side effect first: ``get_backend_class`` /
    ``_populate_registry`` load only the built-in ladder, but the prelude can
    run before ``build_remote_backend`` (which triggers the plugin import) has
    been reached. An unregistered name conservatively returns ``True`` — the
    SSH path is the safe default, and a genuinely unknown backend fails later
    at construction with a clearer error than a flipped capability would give.
    """
    if name in registered_backend_names():
        return get_backend_class(name).requires_ssh
    return True


def registered_backend_names() -> frozenset[str]:
    """Every backend name currently registered, plugin backends included.

    Populates the built-in registry, then imports any installed plugin's
    ``primitive_modules`` — the same side-effect import the primitive
    registry performs at CLI startup — so a plugin's ``@register`` call
    has fired before the names are read. Callers (the clusters.yaml
    ``scheduler`` validator) must not depend on whether primitive
    registration already ran in this process.

    A plugin module that fails to import is *skipped, not fatal* — an
    optional plugin must never take down config validation — but the
    failure is surfaced via :func:`warnings.warn` so the operator
    notices. :func:`hpc_agent._kernel.registry.primitive` emits its own
    warning for the same module, but only on the CLI registration path;
    this call site is reached during config validation, which a
    library consumer can drive without ever touching CLI dispatch, so
    it warns here too rather than relying on that path having run. The
    consequence of a skip is the conservative one — the plugin's
    backend name stays unregistered and a clusters.yaml entry naming it
    fails validation with the names that ARE available.
    """
    import warnings

    _populate_registry()
    from hpc_agent._kernel.registry.plugins import plugin_primitive_modules

    for modname in plugin_primitive_modules():
        try:
            importlib.import_module(modname)
        except Exception as exc:  # noqa: BLE001 — broken plugin must not crash the host
            if modname not in _WARNED_BROKEN_PLUGIN_MODULES:
                _WARNED_BROKEN_PLUGIN_MODULES.add(modname)
                warnings.warn(
                    f"hpc-agent plugin backend module {modname!r} failed to import; "
                    f"its backends are unavailable: {exc}",
                    stacklevel=2,
                )
    return frozenset(_REGISTRY)


# Re-export the profile-driven engine at the package root. Imported at the
# bottom (after ``HPCBackend`` and the registry are defined) so the engine's
# ``from hpc_agent.infra.backends import HPCBackend`` resolves against this
# partially-initialised module without a circular-import failure.
from hpc_agent.infra.backends._engine import (  # noqa: E402
    ProfileBackend,
    RemoteProfileBackend,
)


def build_backend_class(profile: Any, *, remote: bool = True) -> type[HPCBackend]:
    """Synthesise a backend *class* bound to *profile*.

    The class derives its capability metadata (``scheduler_name`` /
    ``template_ext`` / ``supports_test_only_eta`` / ``JOB_ID_REGEX``) from
    the profile via ``ProfileBackend.__init_subclass__``. With
    ``remote=True`` (the default) the SSH transport mixin is placed first
    in the MRO so ``_execute_command`` / ``_setup_log_dir`` run over SSH —
    matching how the golden ``slurm`` / ``sge`` labels submit.

    Used by :func:`register_profile` to wire a *resolved* (LLM-authored or
    seed-from-golden) scheduler profile into the registry at cluster-setup
    time. The two golden labels keep their hand-written
    ``RemoteSlurmBackend`` / ``RemoteSGEBackend`` (imported by name from
    ``remote_factory``); this covers every other resolved profile.
    """
    from hpc_agent.infra.backends._engine import ProfileBackend, RemoteProfileBackend

    if remote:
        from hpc_agent.infra.backends._remote_base import RemoteHPCBackend

        bases: tuple[type, ...] = (RemoteHPCBackend, RemoteProfileBackend)
    else:
        bases = (ProfileBackend,)
    safe = "".join(part.title() for part in str(profile.name).replace("-", "_").split("_"))
    cls_name = f"{'Remote' if remote else ''}{safe or 'Profile'}Backend"
    return type(cls_name, bases, {"profile": profile})


def register_profile(profile: Any, *, remote: bool = True) -> type[HPCBackend]:
    """Register a resolved scheduler *profile* under ``profile.name``.

    After this, ``get_backend_class(profile.name)`` and
    ``get_backend(profile.name, ...)`` return a class bound to *profile*.

    Idempotent for an *equivalent* profile: a name already mapped to an
    equal profile (e.g. the golden ``slurm`` / ``sge`` registered via
    their dedicated remote classes, re-seeded by the resolver) is left
    untouched. A name already mapped to a *different* profile raises
    :class:`~hpc_agent.errors.SpecInvalid` rather than silently
    overwriting — two clusters claiming the same scheduler label with
    divergent profiles is a configuration error that would otherwise make
    backend selection order-dependent. Give each distinct profile its own
    ``name``.
    """
    _populate_registry()
    existing = _REGISTRY.get(profile.name)
    if existing is not None:
        existing_profile = getattr(existing, "profile", None)
        if existing_profile == profile:
            return existing  # idempotent re-registration of the same profile
        raise errors.SpecInvalid(
            f"scheduler label {profile.name!r} is already registered with a "
            "different profile; refusing to silently override it. Two clusters "
            "with divergent schedulers must use distinct scheduler names."
        )
    cls = build_backend_class(profile, remote=remote)
    _REGISTRY[profile.name] = cls
    return cls
