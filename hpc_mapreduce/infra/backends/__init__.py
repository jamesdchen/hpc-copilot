"""
Pluggable HPC backend system.

Provides an abstract interface for job submission so any project
can target any scheduler (SLURM, SGE, PBS, ...) without changing
the core submission logic.

Usage:
    from hpc_mapreduce.infra.backends import get_backend
    backend = get_backend("slurm", script="path/to/job.slurm")
    backend.submit_array(job_name, total_tasks, tasks_per_array, job_env)
"""

from __future__ import annotations

__all__ = [
    "HPCBackend",
    "get_backend",
    "register",
]

import abc
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from hpc_mapreduce.job.throughput import JobBatch, SubmissionPlan


class HPCBackend(abc.ABC):
    """Minimal interface for HPC job submission backends.

    Subclasses implement ``_build_command`` to construct the scheduler-specific
    command.  Override ``_execute_command`` to change how commands are run
    (e.g. via SSH) and ``_setup_log_dir`` for remote ``mkdir``.
    """

    log_dir: str  # subclasses must set this

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

    def _execute_command(
        self,
        cmd: list[str],
        job_env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a scheduler command.  Override for remote execution."""
        return subprocess.run(cmd, env=job_env, cwd=cwd, capture_output=True, text=True)

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
                match = re.search(r"(\d+)", result.stdout)
                if not match:
                    raise RuntimeError(
                        f"Could not parse job ID from output: {result.stdout!r}"
                    )
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
                match = re.search(r"(\d+)", result.stdout)
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


def get_backend(name: str = "slurm", **kwargs: object) -> HPCBackend:
    """Instantiate a backend by name.  *kwargs* are forwarded to the constructor."""
    # Lazy imports to populate registry
    from hpc_mapreduce.infra.backends import sge as _sge  # noqa: F401
    from hpc_mapreduce.infra.backends import sge_remote as _sge_remote  # noqa: F401
    from hpc_mapreduce.infra.backends import slurm as _slurm  # noqa: F401

    if name not in _REGISTRY:
        raise ValueError(f"Unknown backend {name!r}. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)
