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

from hpc_agent.infra.backends.sge import SGEBackend
from hpc_agent.infra.backends.slurm import SlurmBackend
from hpc_agent.infra.throughput import JobBatch, SubmissionPlan

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
        self.envs: list[dict[str, str]] = []
        self._responder = responder

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        self.envs.append(dict(kwargs.get("env") or {}))
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
        monkeypatch.setattr("hpc_agent.infra.backends.subprocess.run", recorder)

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

        # Submissions are (wave, task_range, job_id) tuples (#339).
        assert [s[0] for s in submissions[:3]] == [0, 0, 0]
        assert [s[0] for s in submissions[3:]] == [1, 1, 1]

        # Wave-0 job IDs were 1001, 1002, 1003.
        wave0_jids = [s[2] for s in submissions[:3]]
        assert wave0_jids == ["1001", "1002", "1003"]

        # Every wave-0 argv must carry the sbatch binary and the --array flag,
        # and must NOT carry --dependency.
        for argv in wave0:
            assert argv[0] == "sbatch"
            assert "--array" in argv
            assert not any("--dependency" in a for a in argv)

        # SLURM is index-bounded, so each batch submits a LOCAL ``1-<size>``
        # array (always within MaxArraySize) and ships its global start as
        # TASK_OFFSET in the job env (carried by ``--export``); wave-0 batch 0
        # (offset 0) omits it, staying byte-identical to a ≤cap sweep. The
        # RETURNED submissions still carry the GLOBAL range (asserted at the end).
        assert [argv[argv.index("--array") + 1] for argv in wave0] == ["1-100", "1-100", "1-100"]
        assert [e.get("TASK_OFFSET") for e in recorder.envs[:3]] == [None, "100", "200"]

        # Every wave-1 argv must carry --dependency referencing all wave-0 IDs.
        # Inter-wave chaining is COMPLETION-gated (afterany), not success-gated:
        # the waves are independent slices of one sweep, so a failed task in
        # wave 0 must NOT cancel wave 1 (#339 — afterok would, losing work; it is
        # reserved for the canary gate). No canary here, so no afterok / kill flag.
        for argv in wave1:
            assert "--dependency" in argv
            dep_value = argv[argv.index("--dependency") + 1]
            assert dep_value.startswith("afterany:"), (
                f"Expected 'afterany:' prefix but got {dep_value!r}. "
                "Inter-wave concurrency chaining must be completion-gated so an "
                "independent later wave is not dropped on a partial failure."
            )
            assert "afterok" not in dep_value
            assert not any("--kill-on-invalid-dep=yes" in a for a in argv)
            for jid in wave0_jids:
                assert jid in dep_value

        # Wave 1 argv: also LOCAL ranges; offsets continue 300/400/500.
        assert [argv[argv.index("--array") + 1] for argv in wave1] == ["1-100", "1-100", "1-100"]
        assert [e.get("TASK_OFFSET") for e in recorder.envs[3:]] == ["300", "400", "500"]

        # Returned submissions pair the GLOBAL task_range with each job_id in
        # order (sidecar / wave_map alignment is by global id, not local index).
        assert [s[1] for s in submissions] == [
            "1-100",
            "101-200",
            "201-300",
            "301-400",
            "401-500",
            "501-600",
        ]


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
        monkeypatch.setattr("hpc_agent.infra.backends.subprocess.run", recorder)

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

        # Submissions are (wave, task_range, job_id) tuples (#339).
        assert [s[0] for s in submissions[:3]] == [0, 0, 0]
        assert [s[0] for s in submissions[3:]] == [1, 1, 1]

        # Wave-0 job IDs were 2001, 2002, 2003 (the FIRST integer parsed from
        # stdout — the re.search in submit_plan picks up the leading number).
        wave0_jids = [s[2] for s in submissions[:3]]
        assert wave0_jids == ["2001", "2002", "2003"]

        # Wave 0: qsub with -t, no -hold_jid.
        for argv in wave0:
            assert argv[0] == "qsub"
            assert "-t" in argv
            assert "-hold_jid" not in argv

        # SGE is index-bounded: each batch submits a LOCAL ``1-<size>`` array
        # (within max_aj_tasks) + a per-batch TASK_OFFSET (in the job env);
        # wave-0 batch 0 (offset 0) omits it. Returned submissions keep GLOBAL.
        assert [argv[argv.index("-t") + 1] for argv in wave0] == ["1-100", "1-100", "1-100"]
        assert [e.get("TASK_OFFSET") for e in recorder.envs[:3]] == [None, "100", "200"]

        # Wave 1: -hold_jid (completion gate) with comma-joined wave-0 IDs.
        for argv in wave1:
            assert "-hold_jid" in argv
            hold_value = argv[argv.index("-hold_jid") + 1]
            # SGE convention: comma-separated list.
            assert hold_value == ",".join(wave0_jids)

        assert [argv[argv.index("-t") + 1] for argv in wave1] == ["1-100", "1-100", "1-100"]
        assert [e.get("TASK_OFFSET") for e in recorder.envs[3:]] == ["300", "400", "500"]

        assert [s[1] for s in submissions] == [
            "1-100",
            "101-200",
            "201-300",
            "301-400",
            "401-500",
            "501-600",
        ]
