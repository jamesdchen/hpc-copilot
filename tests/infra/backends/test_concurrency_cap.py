"""Scheduler-native in-array concurrency caps (#339 item 16).

Two layers under test:

* the BACKENDS emit the family-correct cap syntax on an array submission
  (SLURM ``--array=<range>%N``, UGE/SGE ``qsub -tc N``, PBS Pro/TORQUE
  ``-J/-t <range>%N``), only for an array, and are BYTE-IDENTICAL to the
  pre-item-16 command when no cap is passed; and
* the PLANNER records the code-legible concurrency-bounding decision on
  ``SubmissionPlan`` — ``native-cap`` for a pure-concurrency single-array
  sweep, and keeps ``afterany-waves`` / ``concurrent-arrays`` (no native
  replacement) when the array-size ceiling forces a multi-array split.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends import get_backend
from hpc_agent.infra.backends.sge import SGEBackend
from hpc_agent.infra.backends.slurm import SlurmBackend
from hpc_agent.infra.constraints import ClusterConstraints
from hpc_agent.infra.throughput import WorkloadSpec, compute_submission_plan


def _noop_ssh(cmd):  # pragma: no cover - never executed in these unit tests
    raise AssertionError("ssh must not run in a command-shape test")


# ---------------------------------------------------------------------------
# Backend cap syntax — one array, per-family
# ---------------------------------------------------------------------------


def test_slurm_cap_suffixes_array_range(tmp_path):
    backend = SlurmBackend(script=str(tmp_path / "j.slurm"), log_dir=str(tmp_path / "logs"))
    cmd = backend._build_command("1-100", "job", {}, concurrency_cap=20)
    # ``%20`` rides on the --array range token, not a separate flag.
    assert cmd[cmd.index("--array") + 1] == "1-100%20"
    assert "-tc" not in cmd


def test_sge_cap_is_tc_flag(tmp_path):
    backend = SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))
    cmd = backend._build_command("1-100", "job", {}, concurrency_cap=20)
    assert cmd[cmd.index("-t") + 1] == "1-100"  # range unchanged
    assert cmd[cmd.index("-tc") + 1] == "20"  # separate slot-limit flag


def test_torque_cap_suffixes_array_range():
    # TORQUE ``-t`` accepts the ``%N`` slot-limit suffix (cap_style="range_suffix").
    backend = get_backend(
        "torque", script="j.pbs", ssh_run=_noop_ssh, remote_repo="/r", pass_env_keys=("K",)
    )
    cmd = backend._build_command("1-100", "job", {"K": "V"}, concurrency_cap=8)
    assert cmd[cmd.index("-t") + 1] == "1-100%8"
    assert "max_run_subjobs=8" not in " ".join(cmd)


def test_pbspro_cap_is_max_run_subjobs_attr_not_range_suffix():
    # #32: PBS Pro ``-J`` REJECTS the ``%N`` suffix (``qsub: illegal -J value``);
    # the cap is a separate ``-l max_run_subjobs=N`` attribute and the range
    # stays bare (cap_style="max_run_subjobs"). PBS Pro must NOT inherit
    # TORQUE's/SLURM's range-suffix rule.
    backend = get_backend(
        "pbspro", script="j.pbs", ssh_run=_noop_ssh, remote_repo="/r", pass_env_keys=("K",)
    )
    cmd = backend._build_command("1-100", "job", {"K": "V"}, concurrency_cap=8)
    assert cmd[cmd.index("-J") + 1] == "1-100"  # range unchanged, no %8
    assert "1-100%8" not in cmd
    assert cmd[cmd.index("-l") + 1] == "max_run_subjobs=8"


@pytest.mark.parametrize("family,flag", [("pbspro", "-J"), ("torque", "-t")])
def test_pbs_no_cap_is_byte_identical(family, flag):
    # A None / omitted cap emits neither a suffix nor a max_run_subjobs attr.
    backend = get_backend(
        family, script="j.pbs", ssh_run=_noop_ssh, remote_repo="/r", pass_env_keys=("K",)
    )
    bare = backend._build_command("1-100", "job", {"K": "V"})
    assert backend._build_command("1-100", "job", {"K": "V"}, concurrency_cap=None) == bare
    assert backend._build_command("1-100", "job", {"K": "V"}, concurrency_cap=0) == bare
    assert bare[bare.index(flag) + 1] == "1-100"
    assert "max_run_subjobs" not in " ".join(bare)


# ---------------------------------------------------------------------------
# Byte-identical when no cap / when not an array
# ---------------------------------------------------------------------------


def test_no_cap_is_byte_identical(tmp_path):
    slurm = SlurmBackend(script=str(tmp_path / "j.slurm"), log_dir=str(tmp_path / "logs"))
    assert slurm._build_command("1-100", "job", {}) == slurm._build_command(
        "1-100", "job", {}, concurrency_cap=None
    )
    sge = SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))
    assert sge._build_command("1-100", "job", {}) == sge._build_command(
        "1-100", "job", {}, concurrency_cap=None
    )


@pytest.mark.parametrize("cap", [None, 0, 20])
def test_cap_never_emitted_for_non_array_mpi_job(tmp_path, cap):
    """A single non-array (MPI) job carries no cap regardless of the value.

    Compared against the no-cap command so the ``%`` in the log-name pattern
    (``%x_%j_1``) does not read as a phantom concurrency suffix.
    """
    slurm = SlurmBackend(script=str(tmp_path / "j.slurm"), log_dir=str(tmp_path / "logs"))
    baseline = slurm._build_command(None, "job", {}, array=False)
    assert slurm._build_command(None, "job", {}, array=False, concurrency_cap=cap) == baseline
    sge = SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))
    sge_baseline = sge._build_command(None, "job", {}, array=False)
    assert sge._build_command(None, "job", {}, array=False, concurrency_cap=cap) == sge_baseline
    assert "-tc" not in sge_baseline


@pytest.mark.parametrize("cap", [0, -5])
def test_nonpositive_cap_emits_nothing(tmp_path, cap):
    slurm = SlurmBackend(script=str(tmp_path / "j.slurm"), log_dir=str(tmp_path / "logs"))
    cmd = slurm._build_command("1-100", "job", {}, concurrency_cap=cap)
    assert cmd[cmd.index("--array") + 1] == "1-100"
    sge = SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))
    assert "-tc" not in sge._build_command("1-100", "job", {}, concurrency_cap=cap)


# ---------------------------------------------------------------------------
# Planner decision — the disclosed concurrency_mode / concurrency_cap field
# ---------------------------------------------------------------------------


def test_single_array_no_cap_declared_stays_single_array():
    plan = compute_submission_plan(
        ClusterConstraints(max_array_size=1000), WorkloadSpec(total_tasks=50)
    )
    assert plan.total_batches == 1
    assert plan.concurrency_mode == "single-array"
    assert plan.concurrency_cap is None


def test_pure_concurrency_single_array_gets_native_cap():
    """Sweep fits in one array + a concurrency cap below it => native cap, no waves."""
    plan = compute_submission_plan(
        ClusterConstraints(max_array_size=1000, max_concurrent_tasks=20),
        WorkloadSpec(total_tasks=200),
    )
    assert plan.total_batches == 1  # one array — no afterany chain
    assert plan.concurrency_mode == "native-cap"
    assert plan.concurrency_cap == 20
    assert "back-fill" in plan.concurrency_rationale


def test_cap_at_or_above_sweep_size_does_not_restrict():
    """A cap >= the sweep can't bound anything, so it stays byte-identical (no cap)."""
    plan = compute_submission_plan(
        ClusterConstraints(max_array_size=1000, max_concurrent_tasks=500),
        WorkloadSpec(total_tasks=200),
    )
    assert plan.concurrency_mode == "single-array"
    assert plan.concurrency_cap is None


def test_semantic_multiwave_split_keeps_afterany_chain():
    """Over the array ceiling across >1 wave => keep the afterany chain, not a native cap."""
    plan = compute_submission_plan(
        ClusterConstraints(max_array_size=100, max_concurrent_jobs=2),
        WorkloadSpec(total_tasks=1000),  # 10 arrays over 5 waves
    )
    assert plan.total_batches == 10
    assert plan.concurrency_mode == "afterany-waves"
    assert plan.concurrency_cap is None  # no cap declared


def test_multiwave_with_cap_applies_within_each_wave():
    plan = compute_submission_plan(
        ClusterConstraints(max_array_size=100, max_concurrent_jobs=2, max_concurrent_tasks=15),
        WorkloadSpec(total_tasks=1000),
    )
    assert plan.concurrency_mode == "afterany-waves"
    assert plan.concurrency_cap == 15
    assert "within each array" in plan.concurrency_rationale


def test_multi_array_single_wave_is_concurrent_arrays():
    """> ceiling but all arrays fit one wave (<= max_concurrent_jobs) => no afterany."""
    plan = compute_submission_plan(
        ClusterConstraints(max_array_size=100, max_concurrent_jobs=5),
        WorkloadSpec(total_tasks=250),  # 3 arrays, 1 wave
    )
    assert plan.total_batches == 3
    assert plan.concurrency_mode == "concurrent-arrays"


def test_nonpositive_declared_cap_is_rejected():
    with pytest.raises(errors.SpecInvalid, match="max_concurrent_tasks"):
        compute_submission_plan(
            ClusterConstraints(max_concurrent_tasks=0), WorkloadSpec(total_tasks=10)
        )


# ---------------------------------------------------------------------------
# submit-flow wiring: the ≤cap single-array path threads the native cap
# through submit_plan -> submit_one -> _build_command.
# ---------------------------------------------------------------------------


def test_single_array_submission_threads_native_cap(tmp_path):
    import re
    from pathlib import Path
    from types import SimpleNamespace

    from hpc_agent.infra.backends import HPCBackend
    from hpc_agent.ops.submit_flow import _make_single_array_submission

    class _CapRecordingBackend(HPCBackend):
        JOB_ID_REGEX = re.compile(r"JOB(\d+)")

        def __init__(self) -> None:
            self.log_dir = str(tmp_path / "logs")
            self.commands: list[list[str]] = []

        def _build_command(
            self,
            task_range,
            job_name,
            job_env,
            *,
            extra_flags=None,
            array=True,
            concurrency_cap=None,
        ):  # type: ignore[override]
            spec = f"{task_range}%{concurrency_cap}" if concurrency_cap else str(task_range)
            return ["sbatch", "--array", spec, "job.sh"]

        def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
            self.commands.append(list(cmd))
            return SimpleNamespace(stdout="JOB7\n", stderr="", returncode=0)

        def _setup_log_dir(self) -> None:
            pass

    backend = _CapRecordingBackend()
    ids = _make_single_array_submission(
        backend,
        job_name="j",
        total_tasks=200,
        job_env={},
        cwd=Path(str(tmp_path)),
        concurrency_cap=20,
    )
    assert ids == ["7"]
    # The single array carried the native cap suffix, one command only.
    assert backend.commands == [["sbatch", "--array", "1-200%20", "job.sh"]]

    # And with no cap the command is byte-identical to the bare array.
    backend.commands.clear()
    _make_single_array_submission(
        backend, job_name="j", total_tasks=200, job_env={}, cwd=Path(str(tmp_path))
    )
    assert backend.commands == [["sbatch", "--array", "1-200", "job.sh"]]
