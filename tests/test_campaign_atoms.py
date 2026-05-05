"""Smoke tests for the new campaign atoms (advance/budget/converged/replay)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_hpc.atoms.campaign_advance import campaign_advance
from claude_hpc.atoms.campaign_budget import campaign_budget
from claude_hpc.atoms.campaign_converged import campaign_converged
from claude_hpc.atoms.campaign_replay import campaign_replay
from claude_hpc.state.runs import write_run_sidecar


def _seed_run(
    experiment_dir: Path,
    *,
    run_id: str,
    campaign_id: str,
    task_count: int = 1,
) -> None:
    """Write a minimal v2 sidecar so the campaign atoms have something to walk."""
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        claude_hpc_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="hpc_user_tasks",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=task_count,
        tasks_py_sha="0" * 12,
        campaign_id=campaign_id,
        profile="ml",
        cluster="hoffman2",
        remote_path="/u/scratch/exp",
    )


def _seed_metrics(experiment_dir: Path, *, run_id: str, value: float) -> None:
    """Drop a metrics.json under the result dir so prior() picks it up."""
    metrics_dir = experiment_dir / "results" / run_id / "0"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "metrics.json").write_text(json.dumps({"loss": value}))


@pytest.fixture
def campaign_with_history(tmp_path: Path) -> Path:
    for i, loss in enumerate([0.9, 0.5, 0.3, 0.28, 0.275]):
        _seed_run(tmp_path, run_id=f"run_{i:04d}", campaign_id="camp_a")
        _seed_metrics(tmp_path, run_id=f"run_{i:04d}", value=loss)
    return tmp_path


def test_replay_returns_last_n_iterations(campaign_with_history: Path) -> None:
    out = campaign_replay(experiment_dir=campaign_with_history, campaign_id="camp_a", last_n=3)
    assert out["total_iterations"] == 5
    assert out["returned"] == 3
    assert [it["metrics"]["loss"] for it in out["iterations"]] == [0.3, 0.28, 0.275]


def test_converged_target_met(campaign_with_history: Path) -> None:
    out = campaign_converged(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
        metric="loss",
        target=0.4,
        direction="minimize",
    )
    assert out["converged"] is True
    assert "target_met" in out["reason"]


def test_converged_max_iters(campaign_with_history: Path) -> None:
    out = campaign_converged(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
        max_iters=5,
    )
    assert out["converged"] is True
    assert "max_iters_reached" in out["reason"]


def test_converged_plateau(campaign_with_history: Path) -> None:
    # Last 2 improved by only 0.001 — well under the 0.05 tolerance.
    out = campaign_converged(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
        metric="loss",
        plateau_window=2,
        plateau_tolerance=0.05,
    )
    assert out["converged"] is True
    assert "plateau" in out["reason"]


def test_converged_no_criteria(campaign_with_history: Path) -> None:
    out = campaign_converged(experiment_dir=campaign_with_history, campaign_id="camp_a")
    assert out["converged"] is False
    assert out["reason"] == "no_criteria"


def test_budget_exhausted_on_jobs(campaign_with_history: Path) -> None:
    out = campaign_budget(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
        max_jobs=3,
    )
    assert out["spent"]["jobs"] == 5
    assert out["exhausted"] is True
    assert "max_jobs" in out["reason"]


def test_budget_within(campaign_with_history: Path) -> None:
    out = campaign_budget(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
        max_jobs=10,
    )
    assert out["exhausted"] is False
    assert out["remaining"]["max_jobs"] == 5


def test_advance_stops_when_converged(campaign_with_history: Path) -> None:
    out = campaign_advance(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
        metric="loss",
        target=0.4,
    )
    assert out["decision"] == "stop_converged"


def test_advance_stops_over_budget_takes_precedence(campaign_with_history: Path) -> None:
    out = campaign_advance(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
        metric="loss",
        target=0.4,
        max_jobs=3,
    )
    assert out["decision"] == "stop_over_budget"


def test_advance_continue_when_no_criteria(campaign_with_history: Path) -> None:
    out = campaign_advance(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
    )
    assert out["decision"] == "continue"
