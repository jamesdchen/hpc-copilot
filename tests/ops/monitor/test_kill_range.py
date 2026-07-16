"""Range-scoped ``kill`` (M-KILL, SPEC §2 Δ4 / Step E).

A ``kill`` with a ``task_range`` cancels only those array indices — a PARTIAL
cancel by construction: the array stays in flight with its remaining tasks, so
the run is NEVER settled through reconcile (that is the full-kill terminal
transition). The alive-check / verify-gone honesty is unchanged. The one place an
out-of-array index is caught is the kill primitive's range guard (SpecInvalid).
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.kill import KillSpec
from hpc_agent.infra import remote as remote_mod
from hpc_agent.ops.monitor import kill as kill_mod
from hpc_agent.ops.monitor.kill import kill
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _capture_ssh(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []

    def _fake(cmd: str, *, ssh_target: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        sent.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(remote_mod, "ssh_run", _fake)
    return sent


def _patch_reconcile(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record reconcile calls so a test can assert a range kill NEVER settles."""
    calls: list[tuple] = []

    def _fake(experiment_dir, run_id, *, scheduler, **_kw):  # type: ignore[no-untyped-def]
        calls.append((experiment_dir, run_id, scheduler))
        rec = load_run(experiment_dir, run_id)
        assert rec is not None
        return dataclasses.replace(rec, status="abandoned")

    monkeypatch.setattr(kill_mod, "reconcile", _fake)
    return calls


def _record(run_id: str, *, job_ids: list[str], total_tasks: int) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=job_ids,
        total_tasks=total_tasks,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status="in_flight",
    )


def test_range_kill_dispatches_slurm_bracket_and_stays_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SLURM range kill emits ``scancel <id>_[<range>]`` and does NOT settle."""
    sent = _capture_ssh(monkeypatch)
    reconcile_calls = _patch_reconcile(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["555"], total_tasks=100))
    # The array job is still alive (only some indices were cancelled).
    monkeypatch.setattr(
        kill_mod, "_ssh_alive_job_ids", lambda *, ssh_target, job_ids, scheduler: {"555"}
    )

    out = kill(
        experiment_dir=tmp_path,
        spec=KillSpec(run_id="r1", scheduler="slurm", task_range="4,8,13-15"),
    )

    # The range cancel is the SLURM subscript form over the array job id.
    assert sent == ["scancel 555_[4,8,13-15]"]
    assert out["backend_cancel_attempted"] is True
    assert out["backend_cancel_available"] is True
    # PARTIAL by construction: never settled, run left in flight.
    assert reconcile_calls == []
    assert out["settled"] is False
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    assert rec.status == "in_flight"


def test_range_kill_dispatches_sge_dash_t(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An SGE range kill emits ``qdel <id> -t <range>``."""
    sent = _capture_ssh(monkeypatch)
    _patch_reconcile(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["12345"], total_tasks=900))
    monkeypatch.setattr(
        kill_mod, "_ssh_alive_job_ids", lambda *, ssh_target, job_ids, scheduler: {"12345"}
    )

    out = kill(
        experiment_dir=tmp_path,
        spec=KillSpec(run_id="r1", scheduler="sge", task_range="4,8,13-15"),
    )

    assert sent == ["qdel 12345 -t 4,8,13-15"]
    assert out["settled"] is False


def test_range_kill_never_settles_even_if_job_id_reports_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle gate is the RANGE, not the alive result.

    Even if the (job-id-granular) alive check reports the array id gone, a range
    kill is partial by construction and must not route through reconcile — the
    run's remaining tasks are still in flight.
    """
    _capture_ssh(monkeypatch)
    reconcile_calls = _patch_reconcile(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["555"], total_tasks=100))
    # Alive check returns EMPTY (would be a full-kill settle without the range).
    monkeypatch.setattr(
        kill_mod, "_ssh_alive_job_ids", lambda *, ssh_target, job_ids, scheduler: set()
    )

    out = kill(
        experiment_dir=tmp_path,
        spec=KillSpec(run_id="r1", scheduler="slurm", task_range="1-3"),
    )

    assert reconcile_calls == []
    assert out["settled"] is False
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    assert rec.status == "in_flight"


def test_range_index_outside_array_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index beyond the run's array is SpecInvalid — the ONE place caught.

    The guard fires BEFORE any journaled intent or scheduler mutation, so a bad
    range leaves the record untouched.
    """
    sent = _capture_ssh(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["555"], total_tasks=10))

    with pytest.raises(errors.SpecInvalid, match=r"outside the array"):
        kill(
            experiment_dir=tmp_path,
            spec=KillSpec(run_id="r1", scheduler="slurm", task_range="4,8,13-15"),
        )

    # No scheduler mutation and no journaled intent for a refused range.
    assert sent == []
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    assert rec.kill_requested_at is None


def test_range_index_zero_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Array indices are 1-based; index 0 is below the array floor."""
    _capture_ssh(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["555"], total_tasks=100))

    with pytest.raises(errors.SpecInvalid, match=r"outside the array"):
        kill(
            experiment_dir=tmp_path,
            spec=KillSpec(run_id="r1", scheduler="slurm", task_range="0-3"),
        )


def test_in_bounds_range_at_the_ceiling_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary index == total_tasks is inside the array."""
    sent = _capture_ssh(monkeypatch)
    _patch_reconcile(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["555"], total_tasks=15))
    monkeypatch.setattr(
        kill_mod, "_ssh_alive_job_ids", lambda *, ssh_target, job_ids, scheduler: {"555"}
    )

    out = kill(
        experiment_dir=tmp_path,
        spec=KillSpec(run_id="r1", scheduler="slurm", task_range="13-15"),
    )
    assert sent == ["scancel 555_[13-15]"]
    assert out["settled"] is False


def test_malformed_task_range_rejected_at_spec_construction() -> None:
    """KillSpec refuses a value that is not a scheduler array expression."""
    with pytest.raises(ValueError, match=r"scheduler array expression"):
        KillSpec(run_id="r1", scheduler="slurm", task_range="not-a-range")


def test_task_range_forbids_extra_fields() -> None:
    """extra=forbid is preserved on the enriched spec."""
    with pytest.raises(ValueError):
        KillSpec(run_id="r1", scheduler="slurm", bogus="x")  # type: ignore[call-arg]
