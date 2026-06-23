"""Byte-identical equivalence for the ≤cap single-array submit path (#339 inc 3).

Increment 3 collapses ``_make_single_array_submission`` onto the shared backend
wave submitter: for the normal array case it builds a one-batch, one-wave
:class:`SubmissionPlan` and submits it through :meth:`HPCBackend.submit_plan`.
The invariant the increment must hold is that a ≤cap sweep emits the SAME
scheduler command(s) — and returns the same ids — as the pre-#339 inline qsub
(one array ``1-N`` carrying ``resource_flags + extra_flags``).

These tests capture the emitted argv via a stub backend and assert:

* the single-array path emits exactly ONE command, byte-identical to the
  reference ``_build_command(f"1-{N}", ..., extra_flags=resource_flags+extra)``;
* the returned ids match;
* the single multi-rank MPI branch (``array=False``, ``task_range=None``) is
  preserved exactly.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops.submit_flow import _make_single_array_submission


class _RecordingBackend(HPCBackend):
    """Stub backend that records every emitted command and hands back ids.

    ``_build_command`` mirrors the real backends' shape closely enough to make
    the argv assertion meaningful: array flag + task range + name + extra flags.
    ``resource_flags`` returns a fixed sentinel so the test can prove resource
    flags ride the same position they did pre-#339.
    """

    JOB_ID_REGEX = re.compile(r"JOB(\d+)")

    def __init__(self) -> None:
        self.log_dir = "/tmp/equiv-logs"
        self._counter = 200
        self.commands: list[list[str]] = []
        self.log_dir_setups = 0

    def resource_flags(self, resources):  # type: ignore[override]
        return ["--mem", "8G"] if resources is not None else []

    def _build_command(
        self,
        task_range,
        job_name,
        job_env,
        *,
        extra_flags=None,
        array=True,
    ):  # type: ignore[override]
        # array=False is the single non-array MPI job (no ``-t`` range).
        cmd = ["qsub", "-t", str(task_range), "-N", job_name] if array else ["qsub", "-N", job_name]
        cmd.extend(extra_flags or [])
        cmd.append("job.sh")
        return cmd

    def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
        self.commands.append(list(cmd))
        self._counter += 1
        return SimpleNamespace(stdout=f"submitted JOB{self._counter}\n", stderr="", returncode=0)

    def _setup_log_dir(self) -> None:
        self.log_dir_setups += 1


def _reference_command(backend, *, total_tasks, job_name, resources, extra_flags):
    """The pre-#339 inline qsub: ONE array ``1-N`` with resource+extra flags."""
    flags = backend.resource_flags(resources) + list(extra_flags or [])
    return backend._build_command(f"1-{total_tasks}", job_name, {}, extra_flags=flags)


def test_single_array_argv_byte_identical_no_resources() -> None:
    backend = _RecordingBackend()
    reference = _reference_command(
        _RecordingBackend(), total_tasks=50, job_name="probe", resources=None, extra_flags=None
    )

    ids = _make_single_array_submission(
        backend,
        job_name="probe",
        total_tasks=50,
        job_env={},
        cwd=Path("."),
    )

    assert len(backend.commands) == 1
    assert backend.commands[0] == reference
    assert backend.commands[0] == ["qsub", "-t", "1-50", "-N", "probe", "job.sh"]
    assert ids == ["201"]


def test_single_array_argv_byte_identical_with_resources_and_extra() -> None:
    backend = _RecordingBackend()
    resources = SimpleNamespace(mpi=None)  # truthy resources, no mpi -> array path
    extra = ["--dependency", "afterok:999"]
    reference = _reference_command(
        _RecordingBackend(),
        total_tasks=128,
        job_name="probe",
        resources=resources,
        extra_flags=extra,
    )

    ids = _make_single_array_submission(
        backend,
        job_name="probe",
        total_tasks=128,
        job_env={},
        cwd=Path("."),
        resources=resources,
        extra_flags=extra,
    )

    assert len(backend.commands) == 1
    assert backend.commands[0] == reference
    # Resource flags precede the afterok extra flags, exactly as before.
    assert backend.commands[0] == [
        "qsub",
        "-t",
        "1-128",
        "-N",
        "probe",
        "--mem",
        "8G",
        "--dependency",
        "afterok:999",
        "job.sh",
    ]
    assert ids == ["201"]


def test_single_mpi_job_branch_preserved() -> None:
    """A single multi-rank MPI job stays non-array (array=False, no task range)."""
    backend = _RecordingBackend()
    resources = SimpleNamespace(mpi=SimpleNamespace(ranks=8, ranks_per_node=4))

    ids = _make_single_array_submission(
        backend,
        job_name="mpi-probe",
        total_tasks=1,
        job_env={},
        cwd=Path("."),
        resources=resources,
    )

    assert len(backend.commands) == 1
    # No ``-t`` array flag; the non-array shape with resource flags.
    assert backend.commands[0] == ["qsub", "-N", "mpi-probe", "--mem", "8G", "job.sh"]
    assert ids == ["201"]


def test_log_dir_set_up_exactly_once() -> None:
    backend = _RecordingBackend()
    _make_single_array_submission(
        backend, job_name="probe", total_tasks=10, job_env={}, cwd=Path(".")
    )
    assert backend.log_dir_setups == 1
