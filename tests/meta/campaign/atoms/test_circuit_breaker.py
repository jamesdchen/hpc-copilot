"""Commit-3 loop-safety: circuit breaker on N consecutive iteration failures.

The consecutive-failure signal is derived from existing journal state
(``RunRecord.status``, the same field ``campaign-health`` counts as
``n_failed``) — no new persistence. ``campaign-advance`` emits the new
``stop_circuit_breaker`` terminal decision when the trailing run of
failed/abandoned iterations meets the supplied threshold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent.meta.campaign.atoms.advance import campaign_advance
from hpc_agent.meta.campaign.atoms.circuit_breaker import consecutive_terminal_failures
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def _journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hpc_agent.state import run_record

    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


def _seed_iteration(
    experiment_dir: Path,
    *,
    run_id: str,
    campaign_id: str,
    status: str,
) -> None:
    """Seed a sidecar + a journal RunRecord with an explicit terminal status."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

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
        campaign_id=campaign_id,
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
            campaign_id=campaign_id,
            status=status,
        ),
    )


# ─── helper: consecutive_terminal_failures ──────────────────────────────────


def test_counts_trailing_failures(_journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="complete")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="abandoned")

    from hpc_agent.state.index import find_runs_by_campaign

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 2
    assert out["run_ids"] == ["r2", "r1"]  # newest-first
    assert out["last_status"] == "abandoned"


def test_complete_resets_streak(_journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="complete")

    from hpc_agent.state.index import find_runs_by_campaign

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 0
    assert out["last_status"] == "complete"


def test_in_flight_skipped_not_reset(_journal_home: Path, tmp_path: Path) -> None:
    # A just-submitted retry (in_flight) at the tail must NOT reset a real
    # failing streak before it has a terminal verdict.
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="in_flight")

    from hpc_agent.state.index import find_runs_by_campaign

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 2
    assert out["last_status"] == "failed"


# ─── end-to-end: campaign-advance stop_circuit_breaker ──────────────────────


def test_advance_stops_on_circuit_breaker(_journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="failed")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=3)
    assert out["decision"] == "stop_circuit_breaker"
    assert "consecutive" in out["reason"]
    assert out["circuit_breaker"]["count"] == 3


def test_advance_breaker_under_threshold_continues(_journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=3)
    assert out["decision"] == "continue"
    assert out["circuit_breaker"]["count"] == 2


def test_advance_no_breaker_arg_never_fires(_journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A")
    assert out["decision"] == "continue"
    assert out["circuit_breaker"]["threshold"] is None


def test_advance_breaker_defaults_from_manifest(_journal_home: Path, tmp_path: Path) -> None:
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    write_manifest(tmp_path, campaign_id="A", stop_criteria={"circuit_breaker_failures": 2})
    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A")
    assert out["decision"] == "stop_circuit_breaker"


def test_advance_in_flight_takes_precedence_over_breaker(
    _journal_home: Path, tmp_path: Path
) -> None:
    # An in-flight run means the campaign is still progressing; wait_in_flight
    # must win so we don't halt (and orphan the live job) on a stale streak.
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="in_flight")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=2)
    assert out["decision"] == "wait_in_flight"


def test_init_persists_circuit_breaker(tmp_path: Path) -> None:
    from hpc_agent.meta.campaign.atoms.init import campaign_init
    from hpc_agent.meta.campaign.manifest import read_manifest

    campaign_init(experiment_dir=tmp_path, campaign_id="camp_z", circuit_breaker_failures=4)
    manifest = read_manifest(tmp_path, "camp_z")
    assert manifest is not None
    assert manifest["stop_criteria"]["circuit_breaker_failures"] == 4
