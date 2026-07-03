"""Tests for the ``kill`` mutator (§5 kill semantics).

Request -> journaled -> verified. The alive-check is monkeypatched so no cluster
is touched; the focus is the journaled intent, the honest confirmed-gone count,
and the verification-failure honesty rule.
"""

from __future__ import annotations

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
    """Patch ``remote.ssh_run`` to record dispatched cancel commands (no cluster).

    The cancel path now lights up (``build_cancel_cmd`` exists on the seam), so
    ``_attempt_backend_cancel`` dispatches over SSH. We capture the command
    string instead of touching a real host and return a benign success.
    """
    sent: list[str] = []

    def _fake(cmd: str, *, ssh_target: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        sent.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(remote_mod, "ssh_run", _fake)
    return sent


def _patch_reconcile(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Patch the ``reconcile`` primitive kill delegates a FULL kill to.

    Records ``(experiment_dir, run_id, scheduler)`` per call so a test can assert
    a full kill routed the settle through reconcile (and a partial kill did not),
    without a real SSH reconcile round-trip.
    """
    calls: list[tuple] = []

    def _fake(experiment_dir, run_id, *, scheduler, **_kw):  # type: ignore[no-untyped-def]
        calls.append((experiment_dir, run_id, scheduler))
        return None

    monkeypatch.setattr(kill_mod, "reconcile", _fake)
    return calls


def _record(run_id: str, *, job_ids: list[str]) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=job_ids,
        total_tasks=len(job_ids),
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status="in_flight",
    )


def test_kill_journals_intent_and_reports_confirmed_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _capture_ssh(monkeypatch)
    reconcile_calls = _patch_reconcile(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["100", "200", "300"]))
    # 200 is still alive on the scheduler; 100 and 300 are gone.
    monkeypatch.setattr(
        kill_mod,
        "_ssh_alive_job_ids",
        lambda *, ssh_target, job_ids, scheduler: {"200"},
    )

    out = kill(experiment_dir=tmp_path, spec=KillSpec(run_id="r1", scheduler="slurm"))

    # PARTIAL kill (200 still alive): the run is still live, so kill leaves its
    # status untouched and does NOT settle via reconcile.
    assert reconcile_calls == []
    assert out["settled"] is False
    assert out["requested_count"] == 3
    # Confirmed-gone comes from the alive-check verification (200 still alive),
    # NOT from the cancel command's exit code — the cancel only *requests*.
    assert out["confirmed_count"] == 2
    assert out["confirmed_gone_job_ids"] == ["100", "300"]
    assert out["still_alive_job_ids"] == ["200"]
    assert out["summary"] == "3 requested, 2 confirmed gone"
    # The backend cancel affordance now exists and was dispatched through the seam.
    assert out["backend_cancel_available"] is True
    assert out["backend_cancel_attempted"] is True
    # The dispatched command is the SLURM-correct scancel over all requested ids.
    assert sent == ["scancel 100 200 300"]

    # Intent + verified subset are both durable on the journal record.
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    assert rec.kill_requested_job_ids == ["100", "200", "300"]
    assert rec.kill_confirmed_job_ids == ["100", "300"]
    assert rec.kill_requested_at == out["requested_at"]
    assert rec.kill_confirmed_at == out["confirmed_at"]


def test_kill_dispatches_sge_qdel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam picks the scheduler-correct cancel dialect (SGE → qdel)."""
    sent = _capture_ssh(monkeypatch)
    reconcile_calls = _patch_reconcile(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["100", "200"]))
    monkeypatch.setattr(
        kill_mod, "_ssh_alive_job_ids", lambda *, ssh_target, job_ids, scheduler: set()
    )

    out = kill(experiment_dir=tmp_path, spec=KillSpec(run_id="r1", scheduler="sge"))

    assert sent == ["qdel 100 200"]
    assert out["backend_cancel_available"] is True
    assert out["backend_cancel_attempted"] is True
    assert out["confirmed_count"] == 2
    # FULL kill (nothing still alive): kill settles the terminal transition
    # through reconcile — the single settle definition — exactly once, so the
    # journal is marked terminal and the terminal harvest fires there (not here).
    assert reconcile_calls == [(tmp_path, "r1", "sge")]
    assert out["settled"] is True


def test_kill_counts_nothing_gone_on_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SSH/transport failure must not be read as 'the kill worked'.

    The cancel command is dispatched (and observed), but gone-ness is confirmed
    only by the alive-check — which raises here, so NOTHING is counted gone.
    """
    sent = _capture_ssh(monkeypatch)
    upsert_run(tmp_path, _record("r1", job_ids=["100", "200"]))

    def _boom(*, ssh_target: str, job_ids: list[str], scheduler: str) -> set[str]:
        raise errors.RemoteCommandFailed("alive check failed (rc=255)")

    monkeypatch.setattr(kill_mod, "_ssh_alive_job_ids", _boom)

    out = kill(experiment_dir=tmp_path, spec=KillSpec(run_id="r1", scheduler="slurm"))
    assert out["confirmed_count"] == 0
    assert out["confirmed_gone_job_ids"] == []
    assert out["still_alive_job_ids"] == ["100", "200"]
    assert out["summary"] == "2 requested, 0 confirmed gone"
    # Cancel was still requested even though verification later failed.
    assert sent == ["scancel 100 200"]
    assert out["backend_cancel_attempted"] is True
    # Intent is still journaled even though verification failed.
    rec = load_run(tmp_path, "r1")
    assert rec is not None
    assert rec.kill_requested_job_ids == ["100", "200"]
    assert rec.kill_confirmed_job_ids == []


def test_kill_rejects_missing_record(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        kill(experiment_dir=tmp_path, spec=KillSpec(run_id="nope", scheduler="slurm"))
