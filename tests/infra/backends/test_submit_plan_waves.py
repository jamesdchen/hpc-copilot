"""Wave-sequenced ``HPCBackend.submit_plan`` submitter (#339 increments 2-3).

The revived ``submit_plan`` is the shared, subject-neutral wave submitter:
per :class:`JobBatch` it submits the global ``task_range`` reusing the
``_build_command`` / ``_execute_command`` / ``JOB_ID_REGEX`` triplet, groups
batches by wave, submits waves in order, and chains each wave behind the prior
wave's job ids via a **completion** dependency (``afterany`` — the concurrency
chain must NOT drop later independent waves when one task fails; the
success-only ``afterok`` gate is reserved for the canary, passed via
``gate_job_ids``). Per-wave + inter-wave conditions are merged into one
scheduler flag by ``_build_wave_dependency_flag``.

These tests drive it through a stub backend that captures the emitted commands,
mirroring the fake-backend pattern in ``test_afterok_dependency.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_agent.infra.backends import HPCBackend
from hpc_agent.infra.throughput import JobBatch, SubmissionPlan


class _StubBackend(HPCBackend):
    """Minimal in-memory backend that records the commands it would submit.

    ``_build_command`` encodes the task range and any dependency flags so the
    test can assert on the wave chaining without touching a real scheduler.
    ``_execute_command`` hands back a deterministic, unique job id per call.
    """

    JOB_ID_REGEX = re.compile(r"JOB(\d+)")

    def __init__(self, *, afterok: bool) -> None:
        self.log_dir = "/tmp/stub-logs"
        self._afterok = afterok
        self._counter = 100
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.log_dir_setups = 0

    # -- capability under test -------------------------------------------
    @property
    def supports_afterok(self) -> bool:
        return self._afterok

    def _build_afterok_dependency_flag(self, job_ids: list[str]) -> list[str]:
        if not job_ids:
            return []
        return ["--dependency", "afterok:" + ":".join(job_ids)]

    def _build_dependency_flag(self, job_ids: list[str]) -> list[str]:
        if not job_ids:
            return []
        return ["-hold_jid", ",".join(job_ids)]

    def _build_wave_dependency_flag(
        self, *, afterok_ids: list[str], afterany_ids: list[str]
    ) -> list[str]:
        # Models the real engine: a SLURM-like backend (afterok=True) merges
        # success+completion conditions into one ``--dependency``; an
        # afterok-less backend (afterok=False, SGE-like) collapses everything to
        # a completion-only ``-hold_jid`` on the union.
        if not afterok_ids and not afterany_ids:
            return []
        if self._afterok:
            conds: list[str] = []
            if afterok_ids:
                conds.append("afterok:" + ":".join(afterok_ids))
            if afterany_ids:
                conds.append("afterany:" + ":".join(afterany_ids))
            flags = ["--dependency", ",".join(conds)]
            if afterok_ids:
                flags.append("--kill-on-invalid-dep=yes")
            return flags
        return ["-hold_jid", ",".join(afterok_ids + afterany_ids)]

    # -- triplet the submitter reuses ------------------------------------
    def _build_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        cmd = ["submit", "--array", str(task_range), "-N", job_name]
        cmd.extend(extra_flags or [])
        return cmd

    def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
        self.commands.append(list(cmd))
        self.envs.append(dict(job_env))
        self._counter += 1
        return SimpleNamespace(stdout=f"submitted JOB{self._counter}\n", stderr="", returncode=0)

    def _setup_log_dir(self) -> None:
        self.log_dir_setups += 1


def _three_batch_plan() -> SubmissionPlan:
    """3 batches across 3 waves (max_concurrent_jobs == 1)."""
    batches = [
        JobBatch(batch_index=0, task_start=1, task_end=50, array_size=50, est_wall_s=None, wave=0),
        JobBatch(
            batch_index=1, task_start=51, task_end=100, array_size=50, est_wall_s=None, wave=1
        ),
        JobBatch(
            batch_index=2, task_start=101, task_end=150, array_size=50, est_wall_s=None, wave=2
        ),
    ]
    return SubmissionPlan(
        batches=batches,
        total_tasks=150,
        total_batches=3,
        max_concurrent=1,
        est_total_wall_s=None,
        strategy="test",
    )


def test_three_waves_afterany_chain():
    backend = _StubBackend(afterok=True)
    plan = _three_batch_plan()

    submissions = backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))

    # Three global sub-arrays submitted, one per wave, in order.
    assert backend.log_dir_setups == 1
    assert len(backend.commands) == 3

    # Return shape: (wave, task_range, job_id) tuples.
    assert submissions == [
        (0, "1-50", "101"),
        (1, "51-100", "102"),
        (2, "101-150", "103"),
    ]

    # The stub is index-bounded (uses_global_array_index False), so each wave
    # submits a LOCAL 1-<size> array (always within the scheduler's index cap)
    # plus a per-wave TASK_OFFSET that recovers the global id; wave 0 omits the
    # offset (byte-identical to a ≤cap sweep). The RETURNED submissions above
    # still carry the GLOBAL task_range for sidecar/wave_map alignment.
    ranges = [c[c.index("--array") + 1] for c in backend.commands]
    assert ranges == ["1-50", "1-50", "1-50"]
    offsets = [e.get("TASK_OFFSET") for e in backend.envs]
    assert offsets == [None, "50", "100"]

    # Wave 0 has no dependency.
    assert "--dependency" not in backend.commands[0]

    # Inter-wave chaining is COMPLETION-gated (afterany), not success-gated:
    # an independent wave must not be dropped when a prior wave has a failed
    # task. Wave 1 waits on wave 0's id; wave 2 on wave 1's.
    dep1 = backend.commands[1]
    assert dep1[dep1.index("--dependency") + 1] == "afterany:101"
    dep2 = backend.commands[2]
    assert dep2[dep2.index("--dependency") + 1] == "afterany:102"

    # No canary gate here → no afterok / kill-on-invalid-dep anywhere.
    assert not any("afterok" in tok for c in backend.commands for tok in c)
    assert not any("--kill-on-invalid-dep=yes" in c for c in backend.commands)


def test_global_index_backend_keeps_global_ranges_no_offset():
    """A ``uses_global_array_index`` backend (GHA) submits GLOBAL ranges + no offset.

    The counterpart of the index-bounded default: when the scheduler accepts an
    arbitrary global index space, each batch keeps its global ``task_range`` and
    no ``TASK_OFFSET`` is injected.
    """

    class _GlobalStub(_StubBackend):
        uses_global_array_index = True

    backend = _GlobalStub(afterok=True)
    plan = _three_batch_plan()

    backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))

    assert [c[c.index("--array") + 1] for c in backend.commands] == ["1-50", "51-100", "101-150"]
    assert all("TASK_OFFSET" not in e for e in backend.envs)


def test_multi_batch_wave_chains_on_all_prior_ids():
    """A wave with several batches: the next wave depends on ALL prior ids."""
    batches = [
        JobBatch(batch_index=0, task_start=1, task_end=10, array_size=10, est_wall_s=None, wave=0),
        JobBatch(batch_index=1, task_start=11, task_end=20, array_size=10, est_wall_s=None, wave=0),
        JobBatch(batch_index=2, task_start=21, task_end=30, array_size=10, est_wall_s=None, wave=1),
    ]
    plan = SubmissionPlan(
        batches=batches,
        total_tasks=30,
        total_batches=3,
        max_concurrent=2,
        est_total_wall_s=None,
        strategy="test",
    )
    backend = _StubBackend(afterok=True)

    submissions = backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))

    assert [s[0] for s in submissions] == [0, 0, 1]
    wave0_ids = [s[2] for s in submissions[:2]]
    assert wave0_ids == ["101", "102"]

    # Wave 1's single batch completion-depends on BOTH wave-0 ids.
    dep = backend.commands[2]
    assert dep[dep.index("--dependency") + 1] == "afterany:101:102"


def test_every_wave_gates_on_canary_and_chains_for_concurrency():
    """gate_job_ids success-gates EVERY wave; inter-wave adds the afterany chain.

    The two conditions are merged into one ``--dependency`` (a scheduler accepts
    only one): wave 0 carries just the canary afterok; later waves carry
    ``afterok:<canary>,afterany:<prev>`` plus ``--kill-on-invalid-dep=yes``.
    """
    backend = _StubBackend(afterok=True)
    plan = _three_batch_plan()

    backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."), gate_job_ids=["42"])

    dep0 = backend.commands[0]
    assert dep0[dep0.index("--dependency") + 1] == "afterok:42"
    assert "--kill-on-invalid-dep=yes" in dep0
    dep1 = backend.commands[1]
    assert dep1[dep1.index("--dependency") + 1] == "afterok:42,afterany:101"
    assert "--kill-on-invalid-dep=yes" in dep1
    dep2 = backend.commands[2]
    assert dep2[dep2.index("--dependency") + 1] == "afterok:42,afterany:102"


def test_afterok_less_backend_uses_completion_hold():
    backend = _StubBackend(afterok=False)
    plan = _three_batch_plan()

    submissions = backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))

    assert [s[0] for s in submissions] == [0, 1, 2]
    # No afterok anywhere; the completion-only hold chains the waves instead.
    assert not any("--dependency" in c for c in backend.commands)
    assert "-hold_jid" not in backend.commands[0]
    dep1 = backend.commands[1]
    assert dep1[dep1.index("-hold_jid") + 1] == "101"
    dep2 = backend.commands[2]
    assert dep2[dep2.index("-hold_jid") + 1] == "102"


def test_single_wave_has_no_dependency():
    batches = [
        JobBatch(batch_index=0, task_start=1, task_end=5, array_size=5, est_wall_s=None, wave=0),
        JobBatch(batch_index=1, task_start=6, task_end=10, array_size=5, est_wall_s=None, wave=0),
    ]
    plan = SubmissionPlan(
        batches=batches,
        total_tasks=10,
        total_batches=2,
        max_concurrent=2,
        est_total_wall_s=None,
        strategy="test",
    )
    backend = _StubBackend(afterok=True)

    submissions = backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))

    assert [s[0] for s in submissions] == [0, 0]
    assert not any("--dependency" in c or "-hold_jid" in c for c in backend.commands)


def test_nonzero_returncode_raises_with_command_and_stderr():
    class _FailingBackend(_StubBackend):
        def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
            self.commands.append(list(cmd))
            return SimpleNamespace(stdout="", stderr="scheduler said no", returncode=1)

    backend = _FailingBackend(afterok=True)
    plan = _three_batch_plan()

    with pytest.raises(RuntimeError, match="scheduler said no"):
        backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))


def test_unparseable_job_id_raises():
    class _GarbledBackend(_StubBackend):
        def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
            self.commands.append(list(cmd))
            return SimpleNamespace(stdout="no id here\n", stderr="", returncode=0)

    backend = _GarbledBackend(afterok=True)
    plan = _three_batch_plan()

    with pytest.raises(RuntimeError, match="parse job ID"):
        backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))
