"""Multi-job-id journal + canary×waves relation (#339 increment 4).

Covers the three increment-4 guarantees that ride on the wave submitter:

* **Partial accounting on mid-plan failure** — when a wave fails mid-plan,
  ``submit_plan`` attaches the ids that DID land to the raised exception
  (``partial_submit_results``), mirroring ``_submit_flow_batch_locked``.
* **Canary × waves gating** — the canary afterok dependency gates EVERY wave
  (passed as ``gate_job_ids``), because the inter-wave chain is completion-only
  (``afterany``) and cannot propagate a canary failure on its own. Each wave's
  success-gate and completion-chain are merged into one dependency flag.
* **N ids per run round-trip** — the sidecar pre-stamp + dedup tolerate one job
  id per wave (covered in ``tests/state`` for the storage layer; here we assert
  ``submit_plan`` returns N triples a >cap main run will pre-stamp as N ids).
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_agent.infra.backends import HPCBackend
from hpc_agent.infra.throughput import JobBatch, SubmissionPlan


class _StubBackend(HPCBackend):
    """Records emitted commands; deterministic unique job id per call."""

    JOB_ID_REGEX = re.compile(r"JOB(\d+)")

    def __init__(self, *, afterok: bool, fail_on_call: int | None = None) -> None:
        self.log_dir = "/tmp/stub-logs"
        self._afterok = afterok
        self._counter = 100
        self._fail_on_call = fail_on_call
        self._call = 0
        self.commands: list[list[str]] = []

    @property
    def supports_afterok(self) -> bool:
        return self._afterok

    def _build_afterok_dependency_flag(self, job_ids: list[str]) -> list[str]:
        return ["--dependency", "afterok:" + ":".join(job_ids)] if job_ids else []

    def _build_wave_dependency_flag(self, *, afterok_ids, afterany_ids):  # type: ignore[override]
        if not afterok_ids and not afterany_ids:
            return []
        conds: list[str] = []
        if afterok_ids:
            conds.append("afterok:" + ":".join(afterok_ids))
        if afterany_ids:
            conds.append("afterany:" + ":".join(afterany_ids))
        flags = ["--dependency", ",".join(conds)]
        if afterok_ids:
            flags.append("--kill-on-invalid-dep=yes")
        return flags

    def _build_command(self, task_range, job_name, job_env, *, extra_flags=None, array=True):  # type: ignore[override]
        cmd = ["submit", "--array", str(task_range), "-N", job_name]
        cmd.extend(extra_flags or [])
        return cmd

    def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
        self.commands.append(list(cmd))
        self._call += 1
        if self._fail_on_call is not None and self._call == self._fail_on_call:
            return SimpleNamespace(stdout="", stderr="scheduler rejected wave", returncode=1)
        self._counter += 1
        return SimpleNamespace(stdout=f"submitted JOB{self._counter}\n", stderr="", returncode=0)

    def _setup_log_dir(self) -> None:
        pass


def _wave_plan(n_waves: int) -> SubmissionPlan:
    batches = [
        JobBatch(
            batch_index=i,
            task_start=i * 50 + 1,
            task_end=(i + 1) * 50,
            array_size=50,
            est_wall_s=None,
            wave=i,
        )
        for i in range(n_waves)
    ]
    return SubmissionPlan(
        batches=batches,
        total_tasks=n_waves * 50,
        total_batches=n_waves,
        max_concurrent=1,
        est_total_wall_s=None,
        strategy="test",
    )


# --------------------------------------------------------------------------- #
# Canary × waves: the canary gates EVERY wave; inter-wave is afterany.
# --------------------------------------------------------------------------- #


def test_canary_gates_every_wave_with_afterany_chain() -> None:
    backend = _StubBackend(afterok=True)
    plan = _wave_plan(3)

    # The canary's job id "5" success-gates every wave (gate_job_ids).
    submissions = backend.submit_plan(
        plan,
        job_name="probe",
        job_env={},
        cwd=Path("."),
        gate_job_ids=["5"],
    )

    assert [w for w, _r, _j in submissions] == [0, 1, 2]
    wave_ids = [j for _w, _r, j in submissions]

    # Every wave success-gates on the canary (so a canary failure drops the whole
    # sweep — the completion-only afterany chain can't propagate that on its own).
    # Wave 0: just the canary. Later waves: canary AND completion of the prior
    # wave, merged into ONE --dependency.
    dep0 = backend.commands[0]
    assert dep0[dep0.index("--dependency") + 1] == "afterok:5"
    assert "--kill-on-invalid-dep=yes" in dep0
    dep1 = backend.commands[1]
    assert dep1[dep1.index("--dependency") + 1] == f"afterok:5,afterany:{wave_ids[0]}"
    assert "--kill-on-invalid-dep=yes" in dep1
    dep2 = backend.commands[2]
    assert dep2[dep2.index("--dependency") + 1] == f"afterok:5,afterany:{wave_ids[1]}"


def test_no_canary_means_wave0_has_no_dependency() -> None:
    backend = _StubBackend(afterok=True)
    plan = _wave_plan(2)
    submissions = backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))
    assert len(submissions) == 2
    assert "--dependency" not in backend.commands[0]
    # Wave 1 still completion-chains off wave 0 (afterany, no canary).
    dep1 = backend.commands[1]
    assert dep1[dep1.index("--dependency") + 1].startswith("afterany:")
    assert "afterok" not in " ".join(dep1)


# --------------------------------------------------------------------------- #
# Partial accounting on mid-plan failure.
# --------------------------------------------------------------------------- #


def test_mid_plan_failure_attaches_partial_submit_results() -> None:
    # Fail on the 3rd submission (wave 2): waves 0 and 1 already landed.
    backend = _StubBackend(afterok=True, fail_on_call=3)
    plan = _wave_plan(4)

    with pytest.raises(RuntimeError) as exc:
        backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))

    landed = exc.value.partial_submit_results  # type: ignore[attr-defined]
    # Two waves landed before the failure; their ids are recoverable.
    assert [w for w, _r, _j in landed] == [0, 1]
    assert exc.value.failed_wave == 2  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# N ids per run: a >cap main run returns one id per wave.
# --------------------------------------------------------------------------- #


def test_multi_wave_returns_one_id_per_wave() -> None:
    backend = _StubBackend(afterok=True)
    plan = _wave_plan(5)
    submissions = backend.submit_plan(plan, job_name="probe", job_env={}, cwd=Path("."))
    ids = [j for _w, _r, j in submissions]
    assert len(ids) == 5
    assert len(set(ids)) == 5  # no collisions
