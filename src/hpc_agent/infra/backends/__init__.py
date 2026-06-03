"""
Pluggable HPC backend system.

Provides an abstract interface for job submission so any project
can target any scheduler (SLURM, SGE, PBS, ...) without changing
the core submission logic.

Usage:
    from hpc_agent.infra.backends import get_backend
    backend = get_backend("slurm", script="path/to/job.slurm")
    backend.submit_array(job_name, total_tasks, tasks_per_array, job_env)
"""

from __future__ import annotations

__all__ = [
    "HPCBackend",
    "ProfileBackend",
    "RemoteProfileBackend",
    "build_backend_class",
    "get_backend",
    "get_backend_class",
    "register",
    "register_profile",
    "template_ext_for",
]

import abc
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors

if TYPE_CHECKING:
    from collections.abc import Callable

    from hpc_agent.infra.throughput import JobBatch, SubmissionPlan


# Default fallback regex for parsing job IDs out of scheduler stdout.
# Backends override ``JOB_ID_REGEX`` with a scheduler-specific anchor so
# that warnings or banners containing digits (``sbatch: warning: 30%
# pre-empt; Submitted batch job 12345``) don't poison the parse with a
# stray ``30``.
_DEFAULT_JOB_ID_REGEX = re.compile(r"(\d+)")

# Default subprocess timeout for ``qsub``/``sbatch`` invocations.  A
# hung scheduler binary (NFS stall, scheduler outage) would otherwise
# block the agent indefinitely; we surface ``TimeoutExpired`` so callers
# can map it to a cluster-category error.
SUBMIT_TIMEOUT_SEC = 120


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

    def inspect(self, cluster_name: str, **kwargs: Any) -> Any:
        """Return a :class:`ClusterSnapshot` for *cluster_name*.

        Wraps :func:`hpc_agent.infra.inspect.inspect_cluster`'s
        existing per-scheduler dispatch. Subclasses override; the
        default raises so an unmigrated backend is loud.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement inspect")

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

    @abc.abstractmethod
    def _build_command(
        self,
        task_range: str,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
    ) -> list[str]:
        """Return the scheduler command for the given task range."""
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

    def submit_plan(
        self,
        plan: SubmissionPlan,
        job_name: str,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
    ) -> list[tuple[str, str]]:
        """Submit batches according to a SubmissionPlan using scheduler dependencies.

        Returns (task_range, job_id) pairs.
        """
        cwd = cwd or Path.cwd()
        self._setup_log_dir()

        # Group batches by wave
        waves: dict[int, list[JobBatch]] = defaultdict(list)
        for batch in plan.batches:
            waves[batch.wave].append(batch)

        submissions: list[tuple[str, str]] = []
        prev_wave_ids: list[str] = []

        for wave_num in sorted(waves):
            current_wave_ids: list[str] = []
            dep_flags = self._build_dependency_flag(prev_wave_ids) if wave_num > 0 else []

            for batch in waves[wave_num]:
                cmd = self._build_command(
                    batch.task_range, job_name, job_env, extra_flags=dep_flags
                )
                result = self._execute_command(cmd, job_env, cwd)
                if result.returncode != 0:
                    stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
                    raise RuntimeError(
                        f"Job submission failed (exit {result.returncode}) "
                        f"for array {batch.task_range}:\n"
                        f"  command: {' '.join(cmd)}\n"
                        f"  stderr:  {stderr_msg}"
                    )
                match = self.JOB_ID_REGEX.search(result.stdout)
                if not match:
                    raise RuntimeError(f"Could not parse job ID from output: {result.stdout!r}")
                job_id = match.group(1)
                current_wave_ids.append(job_id)
                submissions.append((batch.task_range, job_id))

            prev_wave_ids = current_wave_ids

        return submissions

    def submit_array(
        self,
        job_name: str,
        total_tasks: int,
        tasks_per_array: int,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
    ) -> None:
        """Submit an array job in batches of *tasks_per_array*.

        Parameters
        ----------
        cwd : Path | None
            Working directory for the subprocess.  Defaults to the current
            working directory when ``None``.
        """
        self._run_batches(job_name, total_tasks, tasks_per_array, job_env, cwd=cwd)

    def submit_array_tracked(
        self,
        job_name: str,
        total_tasks: int,
        tasks_per_array: int,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
    ) -> list[tuple[str, str]]:
        """Like submit_array but returns (task_range, job_id) pairs.

        Parameters
        ----------
        cwd : Path | None
            Working directory for the subprocess.  Defaults to the current
            working directory when ``None``.
        """
        return self._run_batches(
            job_name, total_tasks, tasks_per_array, job_env, cwd=cwd, track=True
        )

    def _run_batches(
        self,
        job_name: str,
        total_tasks: int,
        tasks_per_array: int,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
        track: bool = False,
    ) -> list[tuple[str, str]]:
        """Core batching loop shared by submit_array and submit_array_tracked."""
        if tasks_per_array < 1:
            raise errors.SpecInvalid(f"tasks_per_array must be >= 1, got {tasks_per_array}")
        cwd = cwd or Path.cwd()
        self._setup_log_dir()
        submissions: list[tuple[str, str]] = []

        start_task = 1
        while start_task <= total_tasks:
            end_task = min(start_task + tasks_per_array - 1, total_tasks)
            task_range = f"{start_task}-{end_task}"
            cmd = self._build_command(task_range, job_name, job_env)
            result = self._execute_command(cmd, job_env, cwd)
            if result.returncode != 0:
                stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
                raise RuntimeError(
                    f"Job submission failed (exit {result.returncode}) for array {task_range}:\n"
                    f"  command: {' '.join(cmd)}\n"
                    f"  stderr:  {stderr_msg}"
                )
            if track:
                match = self.JOB_ID_REGEX.search(result.stdout)
                if not match:
                    raise RuntimeError(f"Could not parse job ID from output: {result.stdout!r}")
                submissions.append((task_range, match.group(1)))
            start_task = end_task + 1

        return submissions


_REGISTRY: dict[str, type[HPCBackend]] = {}


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
