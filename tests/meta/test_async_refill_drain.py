"""Unit E (#362) — the async ladder DRAINS in-flight runs before a terminal stop.

The RFC invariant "never orphan cluster jobs" survives the async rewrite via
``advance.py::_drain_before_stop``: with ``async_refill`` on, a pending TERMINAL
stop (circuit breaker / resubmit cap / convergence) does NOT fire while runs are
still in flight — the ladder emits ``wait_in_flight`` until the pool empties, then
the same stop rule fires. A budget halt is EXCLUDED from the drain (``_over_budget``
is ordered before ``_drain_before_stop`` and, like the sync ladder, does not wait):
an ack-gated budget cap stops immediately even with runs in flight.

These pins use REAL journal state for the load-bearing dimension being drained
(the in-flight run is a real ``RunRecord`` with ``status="in_flight"``; the breaker
streak is real terminal-failed records) so the drain is exercised against the same
``find_runs_by_campaign`` state the actor reads — mirroring
``tests/.../atoms/test_circuit_breaker.py``. Convergence, which has no cheap
real-journal trigger independent of the iteration count, is injected at its atom.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.meta.campaign.atoms.advance import campaign_advance

if TYPE_CHECKING:
    from pathlib import Path

_CID = "A"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hpc_agent.state import run_record

    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


def _seed_iteration(experiment_dir: Path, *, run_id: str, status: str) -> None:
    """Seed a real sidecar + journal RunRecord for campaign ``_CID``."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        hpc_agent_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="hpc_user_tasks",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=1,
        tasks_py_sha="0" * 12,
        campaign_id=_CID,
        profile="ml",
        cluster="hoffman2",
        remote_path="/u/scratch/exp",
    )
    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/scratch/exp",
            job_name="ml",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment_dir.resolve()),
            campaign_id=_CID,
            status=status,
        ),
    )


# ─── circuit-breaker drain (real journal) ───────────────────────────────────


def test_breaker_stop_drains_while_in_flight(journal_home: Path, tmp_path: Path) -> None:
    """A tripped breaker with a run STILL in flight drains (wait_in_flight) rather
    than orphaning the live job — even though the terminal stop is pending."""
    _seed_iteration(tmp_path, run_id="r0", status="failed")
    _seed_iteration(tmp_path, run_id="r1", status="failed")  # streak = 2 == threshold
    _seed_iteration(tmp_path, run_id="r2", status="in_flight")  # skipped by breaker count

    out = campaign_advance(
        experiment_dir=tmp_path,
        campaign_id=_CID,
        async_refill=True,
        max_in_flight=4,
        circuit_breaker_failures=2,
    )
    # Terminal stop is PENDING (streak met) but a run is in flight → drain first.
    assert out["decision"] == "wait_in_flight"
    assert out["refill_count"] is None
    assert out["circuit_breaker"]["count"] == 2  # the stop is genuinely armed


def test_breaker_stop_fires_once_pool_drained(journal_home: Path, tmp_path: Path) -> None:
    """Once the pool is empty (in_flight == 0), the same armed breaker finally
    emits stop_circuit_breaker — the terminal stop the drain deferred."""
    _seed_iteration(tmp_path, run_id="r0", status="failed")
    _seed_iteration(tmp_path, run_id="r1", status="failed")
    # No in-flight run this time.

    out = campaign_advance(
        experiment_dir=tmp_path,
        campaign_id=_CID,
        async_refill=True,
        max_in_flight=4,
        circuit_breaker_failures=2,
    )
    assert out["decision"] == "stop_circuit_breaker"
    assert out["refill_count"] is None


# ─── convergence drain (real in-flight run, injected convergence) ────────────


def test_converged_stop_drains_while_in_flight(
    journal_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fired convergence criterion with a real run in flight drains first."""
    _seed_iteration(tmp_path, run_id="r0", status="in_flight")

    def fake_converged(**_kw: Any) -> dict[str, Any]:
        return {"converged": True, "reason": "plateau(window=3)"}

    monkeypatch.setattr(
        "hpc_agent.meta.campaign.atoms.converged.campaign_converged", fake_converged
    )
    out = campaign_advance(
        experiment_dir=tmp_path, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "wait_in_flight"
    assert out["refill_count"] is None


def test_converged_stop_fires_once_pool_drained(
    journal_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the pool empty, the fired convergence criterion emits stop_converged."""
    # One completed iteration, nothing in flight.
    _seed_iteration(tmp_path, run_id="r0", status="complete")

    def fake_converged(**_kw: Any) -> dict[str, Any]:
        return {"converged": True, "reason": "plateau(window=3)"}

    monkeypatch.setattr(
        "hpc_agent.meta.campaign.atoms.converged.campaign_converged", fake_converged
    )
    out = campaign_advance(
        experiment_dir=tmp_path, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "stop_converged"
    assert out["refill_count"] is None


# ─── budget halt does NOT wait (matches sync) ───────────────────────────────


def test_budget_halt_does_not_drain(journal_home: Path, tmp_path: Path) -> None:
    """A met budget cap halts IMMEDIATELY even with a run in flight — the budget
    halt is ordered before the drain and, like the sync ladder, does not wait
    (it is a recoverable, ack-gated stop, not a terminal one)."""
    # One in-flight run whose sidecar is the single spent job → max_jobs=1 met.
    _seed_iteration(tmp_path, run_id="r0", status="in_flight")

    out = campaign_advance(
        experiment_dir=tmp_path,
        campaign_id=_CID,
        async_refill=True,
        max_in_flight=4,
        max_jobs=1,
    )
    assert out["decision"] == "stop_over_budget"
    assert out["needs_acknowledgement"] is True
    assert out["refill_count"] is None


def test_budget_halt_ordered_before_terminal_drain(journal_home: Path, tmp_path: Path) -> None:
    """When budget is met AND a breaker is armed AND a run is in flight, the
    budget halt still wins (it precedes ``_drain_before_stop`` in the async ladder),
    confirming the drain is scoped to TERMINAL stops only."""
    _seed_iteration(tmp_path, run_id="r0", status="failed")
    _seed_iteration(tmp_path, run_id="r1", status="failed")  # breaker armed (>=2)
    _seed_iteration(tmp_path, run_id="r2", status="in_flight")  # in flight + 3rd sidecar

    # 3 sidecars → spent jobs 3 >= max_jobs 3 → budget exhausted.
    out = campaign_advance(
        experiment_dir=tmp_path,
        campaign_id=_CID,
        async_refill=True,
        max_in_flight=4,
        circuit_breaker_failures=2,
        max_jobs=3,
    )
    assert out["decision"] == "stop_over_budget"
    assert out["refill_count"] is None
