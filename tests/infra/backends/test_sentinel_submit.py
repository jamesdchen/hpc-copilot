"""``HPCBackend.submit_sentinel`` — the W1 run-terminal sentinel's qsub edge.

Crash-only-monitoring W1 (``docs/design/crash-only-monitoring.md``): the
sentinel is a tiny NON-array job riding a scheduler COMPLETION dependency
behind the run's array jobs. These tests pin the exact per-family flag strings
(the dependency spec + the minimal walltime ask), the script swap (the staged
sentinel script is the submitted script; the backend instance is never
mutated), and the capability refusals (no-dependency backend, empty id list).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends import SENTINEL_WALLTIME_SEC, HPCBackend
from hpc_agent.infra.backends.sge import SGEBackend
from hpc_agent.infra.backends.slurm import SlurmBackend


def _capture(backend, stdout: str):
    """Monkeypatch-free capture: swap _execute_command on the instance."""
    captured: list[list[str]] = []

    def _exec(cmd, job_env, cwd):
        captured.append(list(cmd))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    backend._execute_command = _exec  # type: ignore[method-assign]
    return captured


def test_slurm_sentinel_command_exact() -> None:
    b = SlurmBackend(script="run.slurm", log_dir="logs")
    captured = _capture(b, "Submitted batch job 999\n")
    job_id = b.submit_sentinel(
        script_path=".hpc/announce/r1.sentinel.sh",
        job_name="myrun_sentinel",
        depend_job_ids=["11", "22"],
        cwd=Path("."),
    )
    assert job_id == "999"
    assert captured == [
        [
            "sbatch",
            "--job-name",
            "myrun_sentinel",
            "--output",
            "logs/%x_%j_1.out",
            "--error",
            "logs/%x_%j_1.err",
            "--time",
            "10",  # SENTINEL_WALLTIME_SEC=600s → 10 min
            "--dependency",
            "afterany:11:22",
            ".hpc/announce/r1.sentinel.sh",
        ]
    ]
    assert SENTINEL_WALLTIME_SEC == 600


def test_sge_sentinel_command_exact() -> None:
    b = SGEBackend(script="run.sh", log_dir="logs")
    captured = _capture(b, 'Your job 4242 ("s") has been submitted\n')
    job_id = b.submit_sentinel(
        script_path=".hpc/announce/r1.sentinel.sh",
        job_name="myrun_sentinel",
        depend_job_ids=["11", "22"],
        cwd=Path("."),
    )
    assert job_id == "4242"
    assert captured == [
        [
            "qsub",
            "-N",
            "myrun_sentinel",
            "-o",
            "logs",
            "-j",
            "y",
            "-l",
            "h_rt=00:10:00",
            "-hold_jid",
            "11,22",
            ".hpc/announce/r1.sentinel.sh",
        ]
    ]


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_sentinel_dependency_flag_exact(family: str) -> None:
    from hpc_agent.infra.backends import build_backend_class
    from hpc_agent.infra.backends.profile import PBSPRO_PROFILE, TORQUE_PROFILE

    profile = PBSPRO_PROFILE if family == "pbspro" else TORQUE_PROFILE
    cls = build_backend_class(profile, remote=False)
    b = cls()  # the profile engine carries no local __init__; set the attrs
    b.script = "run.pbs"
    b.log_dir = "logs"
    b.pass_env_keys = ()
    captured = _capture(b, "77[].server\n")
    job_id = b.submit_sentinel(
        script_path=".hpc/announce/r1.sentinel.sh",
        job_name="myrun_sentinel",
        depend_job_ids=["11", "22"],
        cwd=Path("."),
    )
    assert job_id == "77[]"
    (cmd,) = captured
    # Non-array: no -J / -t. Dependency: one -W depend=afterany flag.
    assert "-J" not in cmd and "-t" not in cmd
    i = cmd.index("-W")
    assert cmd[i + 1] == "depend=afterany:11:22"
    # Minimal walltime, PBS spelling (torque folds it into its one -l list).
    j = cmd.index("-l")
    assert "walltime=00:10:00" in cmd[j + 1]
    assert cmd[-1] == ".hpc/announce/r1.sentinel.sh"


def test_sentinel_is_non_array_and_never_mutates_the_backend() -> None:
    b = SlurmBackend(script="run.slurm", log_dir="logs")
    captured = _capture(b, "Submitted batch job 1\n")
    b.submit_sentinel(
        script_path="sentinel.sh",
        job_name="n",
        depend_job_ids=["5"],
        cwd=Path("."),
    )
    (cmd,) = captured
    assert "--array" not in cmd  # non-array job
    # The clone carried the sentinel script; the real backend is untouched, so
    # a later main/canary submit still ships the run's own script.
    assert b.script == "run.slurm"
    assert cmd[-1] == "sentinel.sh"


def test_sentinel_carries_no_env_export_and_no_correlation_weave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No HPC_RUN_ID rides the sentinel: even with submit-once ON, neither the
    --export env block nor the correlation flag appears — the sentinel must
    never write the run's jobmap wave files or carry its token."""
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    b = SlurmBackend(script="run.slurm", log_dir="logs")
    captured = _capture(b, "Submitted batch job 2\n")
    b.submit_sentinel(
        script_path="sentinel.sh",
        job_name="n",
        depend_job_ids=["5"],
        cwd=Path("."),
    )
    (cmd,) = captured
    assert "--export" not in cmd
    assert not any("HPC_RUN_ID" in tok for tok in cmd)


def test_backend_without_dependency_support_raises_not_implemented() -> None:
    class _NoDeps(HPCBackend):
        def __init__(self) -> None:
            self.log_dir = "logs"
            self.script = "s.sh"

        def _build_command(self, task_range, job_name, job_env, *, extra_flags=None, array=True):
            return ["submit"]

    b = _NoDeps()
    with pytest.raises(NotImplementedError, match="completion-dependency"):
        b.submit_sentinel(
            script_path="sentinel.sh",
            job_name="n",
            depend_job_ids=["5"],
        )


def test_empty_dependency_ids_refused() -> None:
    b = SlurmBackend(script="run.slurm", log_dir="logs")
    with pytest.raises(errors.SpecInvalid, match="dependency job id"):
        b.submit_sentinel(script_path="sentinel.sh", job_name="n", depend_job_ids=[])
    with pytest.raises(errors.SpecInvalid, match="dependency job id"):
        b.submit_sentinel(script_path="sentinel.sh", job_name="n", depend_job_ids=["  "])


def test_scheduler_rejection_propagates_as_runtime_error() -> None:
    b = SlurmBackend(script="run.slurm", log_dir="logs")

    def _exec(cmd, job_env, cwd):
        return SimpleNamespace(stdout="", stderr="Invalid dependency", returncode=1)

    b._execute_command = _exec  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Invalid dependency"):
        b.submit_sentinel(
            script_path="sentinel.sh",
            job_name="n",
            depend_job_ids=["5"],
            cwd=Path("."),
        )
