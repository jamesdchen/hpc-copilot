"""Integration tests for HPCBackend.submit_plan command construction.

Builds a 2-wave x 3-batch SubmissionPlan, mocks subprocess.run, and
asserts:
  * exactly 6 submissions happen (2 waves * 3 batches);
  * wave-1 batches carry the scheduler's array-range flag;
  * wave-2 batches carry dependency flags referencing every wave-1 job ID;
  * each batch's ``task_range`` appears in its argv.
"""

from __future__ import annotations

from types import SimpleNamespace

from claude_hpc.infra.backends.sge import SGEBackend
from claude_hpc.infra.backends.slurm import SlurmBackend
from claude_hpc.orchestrator.planning.throughput import JobBatch, SubmissionPlan

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _make_plan() -> SubmissionPlan:
    """Build a concrete 2-wave x 3-batch plan with distinct task ranges."""
    batches = [
        # Wave 0 (submitted first).
        JobBatch(
            batch_index=0, task_start=1, task_end=100, array_size=100, est_wall_s=None, wave=0
        ),
        JobBatch(
            batch_index=1, task_start=101, task_end=200, array_size=100, est_wall_s=None, wave=0
        ),
        JobBatch(
            batch_index=2, task_start=201, task_end=300, array_size=100, est_wall_s=None, wave=0
        ),
        # Wave 1 (depends on wave 0 completing).
        JobBatch(
            batch_index=3, task_start=301, task_end=400, array_size=100, est_wall_s=None, wave=1
        ),
        JobBatch(
            batch_index=4, task_start=401, task_end=500, array_size=100, est_wall_s=None, wave=1
        ),
        JobBatch(
            batch_index=5, task_start=501, task_end=600, array_size=100, est_wall_s=None, wave=1
        ),
    ]
    return SubmissionPlan(
        batches=batches,
        total_tasks=600,
        total_batches=6,
        max_concurrent=3,
        est_total_wall_s=None,
        strategy="test",
    )


class _Recorder:
    def __init__(self, responder):
        self.calls: list[list[str]] = []
        self._responder = responder

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        return self._responder(cmd)


def _cp(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# ---------------------------------------------------------------------------
# SLURM
# ---------------------------------------------------------------------------


class TestSubmitPlanSlurm:
    def test_six_calls_and_dependency_wiring(self, monkeypatch, tmp_path):
        plan = _make_plan()

        # Hand out deterministic, unique job IDs: 1001..1006 in call order.
        counter = {"n": 1000}

        def responder(cmd):
            counter["n"] += 1
            return _cp(stdout=f"Submitted batch job {counter['n']}\n")

        recorder = _Recorder(responder)
        # Patch subprocess.run at the source used by HPCBackend._execute_command.
        monkeypatch.setattr("claude_hpc.infra.backends.subprocess.run", recorder)

        backend = SlurmBackend(script=str(tmp_path / "job.slurm"), log_dir=str(tmp_path / "logs"))

        submissions = backend.submit_plan(
            plan,
            job_name="probe",
            job_env={"FOO": "bar"},
            cwd=tmp_path,
        )

        # 2 waves * 3 batches = 6 subprocess invocations.
        assert len(recorder.calls) == 6
        assert len(submissions) == 6

        # Split by wave (first 3 calls are wave 0, next 3 are wave 1).
        wave0 = recorder.calls[:3]
        wave1 = recorder.calls[3:]

        # Wave-0 job IDs were 1001, 1002, 1003.
        wave0_jids = [s[1] for s in submissions[:3]]
        assert wave0_jids == ["1001", "1002", "1003"]

        # Every wave-0 argv must carry the sbatch binary and the --array flag,
        # and must NOT carry --dependency.
        for argv in wave0:
            assert argv[0] == "sbatch"
            assert "--array" in argv
            assert not any("--dependency" in a for a in argv)

        # Task ranges in wave 0 argv.
        expected_ranges_w0 = ["1-100", "101-200", "201-300"]
        actual_ranges_w0 = [argv[argv.index("--array") + 1] for argv in wave0]
        assert actual_ranges_w0 == expected_ranges_w0

        # Every wave-1 argv must carry --dependency referencing all wave-0 IDs.
        for argv in wave1:
            assert "--dependency" in argv
            dep_value = argv[argv.index("--dependency") + 1]
            # SlurmBackend uses `afterany:<jid1>:<jid2>:<jid3>`.
            # Plan file recommends `afterany`; code currently uses `afterany`.
            # If this ever changes to `afterok`, the assertion below will flag it.
            assert dep_value.startswith("afterany:"), (
                f"Expected 'afterany:' prefix but got {dep_value!r}. "
                "If this has become 'afterok:', that contradicts the plan "
                "review which recommends afterany."
            )
            for jid in wave0_jids:
                assert jid in dep_value

        # Task ranges in wave 1 argv.
        expected_ranges_w1 = ["301-400", "401-500", "501-600"]
        actual_ranges_w1 = [argv[argv.index("--array") + 1] for argv in wave1]
        assert actual_ranges_w1 == expected_ranges_w1

        # Returned submissions should pair task_range with job_id in order.
        assert [s[0] for s in submissions] == expected_ranges_w0 + expected_ranges_w1


# ---------------------------------------------------------------------------
# SGE
# ---------------------------------------------------------------------------


class TestSubmitPlanSge:
    def test_six_calls_and_dependency_wiring(self, monkeypatch, tmp_path):
        plan = _make_plan()

        counter = {"n": 2000}

        def responder(cmd):
            counter["n"] += 1
            # Mimic qsub array-job stdout.
            return _cp(
                stdout=f'Your job-array {counter["n"]}.1-100:1 ("probe") has been submitted\n'
            )

        recorder = _Recorder(responder)
        monkeypatch.setattr("claude_hpc.infra.backends.subprocess.run", recorder)

        backend = SGEBackend(script=str(tmp_path / "job.sh"), log_dir=str(tmp_path / "logs"))

        submissions = backend.submit_plan(
            plan,
            job_name="probe",
            job_env={"FOO": "bar"},
            cwd=tmp_path,
        )

        assert len(recorder.calls) == 6
        assert len(submissions) == 6

        wave0 = recorder.calls[:3]
        wave1 = recorder.calls[3:]

        # Wave-0 job IDs were 2001, 2002, 2003 (the FIRST integer parsed from
        # stdout — the re.search in submit_plan picks up the leading number).
        wave0_jids = [s[1] for s in submissions[:3]]
        assert wave0_jids == ["2001", "2002", "2003"]

        # Wave 0: qsub with -t, no -hold_jid.
        for argv in wave0:
            assert argv[0] == "qsub"
            assert "-t" in argv
            assert "-hold_jid" not in argv

        expected_ranges_w0 = ["1-100", "101-200", "201-300"]
        actual_ranges_w0 = [argv[argv.index("-t") + 1] for argv in wave0]
        assert actual_ranges_w0 == expected_ranges_w0

        # Wave 1: -hold_jid with comma-joined wave-0 IDs.
        for argv in wave1:
            assert "-hold_jid" in argv
            hold_value = argv[argv.index("-hold_jid") + 1]
            # SGE convention: comma-separated list.
            assert hold_value == ",".join(wave0_jids)

        expected_ranges_w1 = ["301-400", "401-500", "501-600"]
        actual_ranges_w1 = [argv[argv.index("-t") + 1] for argv in wave1]
        assert actual_ranges_w1 == expected_ranges_w1

        assert [s[0] for s in submissions] == expected_ranges_w0 + expected_ranges_w1
