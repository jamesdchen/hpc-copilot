"""Smoke tests for the new campaign atoms (advance/budget/converged/replay)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from claude_hpc.atoms.campaign_advance import campaign_advance
from claude_hpc.atoms.campaign_budget import campaign_budget
from claude_hpc.atoms.campaign_converged import campaign_converged
from claude_hpc.atoms.campaign_replay import campaign_replay
from claude_hpc.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


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


# ─── manifest-defaulting ────────────────────────────────────────────────────


def test_budget_defaults_from_manifest(campaign_with_history: Path) -> None:
    from claude_hpc.campaign.manifest import write_manifest

    write_manifest(
        campaign_with_history,
        campaign_id="camp_a",
        budget={"max_jobs": 3, "max_tasks": None, "max_walltime_sec": None},
    )
    out = campaign_budget(experiment_dir=campaign_with_history, campaign_id="camp_a")
    assert out["budget"]["max_jobs"] == 3
    assert out["exhausted"] is True


def test_cli_arg_wins_over_manifest_budget(campaign_with_history: Path) -> None:
    from claude_hpc.campaign.manifest import write_manifest

    write_manifest(
        campaign_with_history,
        campaign_id="camp_a",
        budget={"max_jobs": 3},
    )
    out = campaign_budget(
        experiment_dir=campaign_with_history,
        campaign_id="camp_a",
        max_jobs=100,  # explicit override
    )
    assert out["budget"]["max_jobs"] == 100
    assert out["exhausted"] is False


def test_converged_defaults_from_manifest(campaign_with_history: Path) -> None:
    from claude_hpc.campaign.manifest import write_manifest

    write_manifest(
        campaign_with_history,
        campaign_id="camp_a",
        stop_criteria={"metric": "loss", "target": 0.4, "direction": "minimize"},
    )
    out = campaign_converged(experiment_dir=campaign_with_history, campaign_id="camp_a")
    assert out["converged"] is True
    assert "target_met" in out["reason"]


def test_advance_uses_manifest_for_both_blocks(campaign_with_history: Path) -> None:
    from claude_hpc.campaign.manifest import write_manifest

    write_manifest(
        campaign_with_history,
        campaign_id="camp_a",
        budget={"max_jobs": 100},
        stop_criteria={"metric": "loss", "target": 0.4, "direction": "minimize"},
    )
    out = campaign_advance(experiment_dir=campaign_with_history, campaign_id="camp_a")
    assert out["decision"] == "stop_converged"


def test_corrupt_manifest_does_not_tank(campaign_with_history: Path) -> None:
    from claude_hpc.campaign.manifest import manifest_path

    path = manifest_path(campaign_with_history, "camp_a")
    path.write_text("{not valid json")
    out = campaign_budget(experiment_dir=campaign_with_history, campaign_id="camp_a")
    assert out["budget"]["max_jobs"] is None


# ─── campaign-init ──────────────────────────────────────────────────────────


def test_campaign_init_writes_manifest(tmp_path: Path) -> None:
    from claude_hpc.atoms.campaign_init import campaign_init
    from claude_hpc.campaign.manifest import read_manifest

    out = campaign_init(
        experiment_dir=tmp_path,
        campaign_id="camp_z",
        goal="lowest val loss",
        max_iters=20,
        metric="loss",
        target=0.1,
        max_jobs=50,
        strategy_name="optuna-tpe",
        strategy_params_json='{"n_startup_trials": 10}',
    )
    assert out["campaign_id"] == "camp_z"
    manifest = read_manifest(tmp_path, "camp_z")
    assert manifest is not None
    assert manifest["goal"] == "lowest val loss"
    assert manifest["budget"]["max_jobs"] == 50
    assert manifest["stop_criteria"]["target"] == 0.1
    assert manifest["strategy"]["name"] == "optuna-tpe"
    assert manifest["strategy"]["params"] == {"n_startup_trials": 10}


def test_campaign_init_minimal(tmp_path: Path) -> None:
    from claude_hpc.atoms.campaign_init import campaign_init
    from claude_hpc.campaign.manifest import read_manifest

    campaign_init(experiment_dir=tmp_path, campaign_id="camp_z")
    manifest = read_manifest(tmp_path, "camp_z")
    assert manifest is not None
    assert "budget" not in manifest
    assert "stop_criteria" not in manifest
    assert "strategy" not in manifest


def test_campaign_init_rejects_non_object_strategy_params(tmp_path: Path) -> None:
    from claude_hpc import errors
    from claude_hpc.atoms.campaign_init import campaign_init

    # campaign-init declares error_codes=[SpecInvalid]; the legacy
    # bare-ValueError raise was rewrapped as part of BUG-2V2-9.
    with pytest.raises(errors.SpecInvalid, match="strategy-params-json must decode"):
        campaign_init(
            experiment_dir=tmp_path,
            campaign_id="camp_z",
            strategy_name="custom",
            strategy_params_json="[1, 2, 3]",
        )
