"""Tests for the validate-stochastic-marker atom.

Catches the closed-loop campaign silent-dedup bug class: stochastic
strategies (Optuna, random-search, PBT) re-picking the same params
across iterations, making cmd_sha collide and submit-flow dedupe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent._internal.session import RunRecord, upsert_run
from hpc_agent._internal.session import run_record as session_run_record
from hpc_agent._schema_models.validators.validate_stochastic_marker import (
    ValidateStochasticMarkerSpec,
)
from hpc_agent.atoms.validate_stochastic_marker import validate_stochastic_marker


def _seed_run_sidecar(
    experiment_dir: Path,
    *,
    run_id: str,
    cmd_sha: str,
    campaign_id: str,
) -> None:
    """Write a minimal sidecar to ``<experiment_dir>/.hpc/runs/<run_id>.json``."""
    runs_dir = experiment_dir / ".hpc" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "sidecar_schema_version": 1,
        "run_id": run_id,
        "cmd_sha": cmd_sha,
        "hpc_agent_version": "0.3.0",
        "submitted_at": "2026-05-07T12:00:00Z",
        "executor": "python3 src/test.py",
        "result_dir_template": "results/{run_id}",
        "task_count": 4,
        "tasks_py_sha": "",
        "wave_map": {"0": [0, 1, 2, 3]},
        "campaign_id": campaign_id,
    }
    (runs_dir / f"{run_id}.json").write_text(json.dumps(payload, indent=2, sort_keys=True))


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HPC_HOMEDIR for the journal lookup the validator does
    via session.find_existing_runs.

    ``find_existing_runs`` actually scans ``<experiment_dir>/.hpc/runs/``
    (the per-experiment sidecar dir, not the journal); the validator
    doesn't need HPC_HOMEDIR redirection. But journal_dir() may still
    be touched as a side-effect of importing session — redirect to be
    safe.
    """
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(session_run_record, "HPC_HOMEDIR", home)
    return home


def _seed_journal_run(experiment_dir: Path, *, run_id: str, campaign_id: str = "") -> None:
    """Seed a journal run record so list-by-campaign queries find it.

    The validator reads sidecars under ``.hpc/runs/`` directly; the
    journal record isn't strictly required, but seeding both mirrors
    what submit-flow actually writes.
    """
    record = RunRecord(
        run_id=run_id,
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path="/scratch/exp",
        job_name="ml",
        job_ids=["12345"],
        total_tasks=4,
        submitted_at="2026-05-07T12:00:00Z",
        experiment_dir=str(experiment_dir),
        campaign_id=campaign_id,
    )
    upsert_run(experiment_dir, record)


class TestValidateStochasticMarker:
    def test_no_prior_iterations_passes(self, journal_home, tmp_path):
        """When no prior iterations of this campaign exist, the validator
        passes silently. The first iteration of a campaign is always
        clean (no prior cmd_sha to collide with)."""
        spec = ValidateStochasticMarkerSpec(
            campaign_id="ml_q1_optuna",
            expected_cmd_sha="abcdef1234567890",
        )
        result = validate_stochastic_marker(tmp_path, spec=spec)
        assert result.findings == []
        assert result.matched_prior_run_ids == []

    def test_no_collision_with_different_cmd_sha(self, journal_home, tmp_path):
        """A prior iteration of the same campaign with a DIFFERENT cmd_sha
        is the typical pass case (stochastic strategy picked different
        params this iteration)."""
        _seed_run_sidecar(
            tmp_path,
            run_id="ml-20260507-100000-aaaaaaa1",
            cmd_sha="aaaaaaa1234567890",
            campaign_id="ml_q1_optuna",
        )
        _seed_journal_run(
            tmp_path, run_id="ml-20260507-100000-aaaaaaa1", campaign_id="ml_q1_optuna"
        )

        spec = ValidateStochasticMarkerSpec(
            campaign_id="ml_q1_optuna",
            expected_cmd_sha="bbbbbbb1234567890",
        )
        result = validate_stochastic_marker(tmp_path, spec=spec)
        assert result.findings == []
        assert result.matched_prior_run_ids == []

    def test_collision_fires_error_finding(self, journal_home, tmp_path):
        """The bug case: a prior iteration of the same campaign has the
        SAME cmd_sha. submit-flow would dedupe silently — the validator
        emits an error finding."""
        _seed_run_sidecar(
            tmp_path,
            run_id="ml-20260507-100000-aaaaaaa1",
            cmd_sha="aaaaaaa1234567890",
            campaign_id="ml_q1_optuna",
        )
        _seed_journal_run(
            tmp_path, run_id="ml-20260507-100000-aaaaaaa1", campaign_id="ml_q1_optuna"
        )

        spec = ValidateStochasticMarkerSpec(
            campaign_id="ml_q1_optuna",
            expected_cmd_sha="aaaaaaa1234567890",  # collides
        )
        result = validate_stochastic_marker(tmp_path, spec=spec)
        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.severity == "error"
        assert f.code == "stochastic_marker_missing"
        assert "ml-20260507-100000-aaaaaaa1" in f.message
        assert "aaaaaaa1" in f.message
        assert f.suggested_fix is not None
        assert "_optuna_trial_number" in (f.suggested_fix or "")
        assert result.matched_prior_run_ids == ["ml-20260507-100000-aaaaaaa1"]

    def test_collision_only_within_same_campaign(self, journal_home, tmp_path):
        """A prior iteration with the same cmd_sha but a DIFFERENT campaign
        does NOT collide. Cross-campaign sharing is fine — the dedup
        engine only fires within the same campaign."""
        _seed_run_sidecar(
            tmp_path,
            run_id="other-20260507-100000-aaaaaaa1",
            cmd_sha="aaaaaaa1234567890",
            campaign_id="other_campaign",
        )
        _seed_journal_run(
            tmp_path, run_id="other-20260507-100000-aaaaaaa1", campaign_id="other_campaign"
        )

        spec = ValidateStochasticMarkerSpec(
            campaign_id="ml_q1_optuna",
            expected_cmd_sha="aaaaaaa1234567890",
        )
        result = validate_stochastic_marker(tmp_path, spec=spec)
        assert result.findings == []
        assert result.matched_prior_run_ids == []

    def test_multiple_collisions_reported(self, journal_home, tmp_path):
        """When N prior iterations all share the same cmd_sha, the
        finding's evidence reports the count and the matched run_ids
        list contains all of them (newest-first)."""
        for _i, run_id in enumerate(
            [
                "ml-20260507-100000-aaaaaaa1",
                "ml-20260507-110000-aaaaaaa1",
                "ml-20260507-120000-aaaaaaa1",
            ]
        ):
            _seed_run_sidecar(
                tmp_path,
                run_id=run_id,
                cmd_sha="aaaaaaa1234567890",
                campaign_id="ml_q1_optuna",
            )
            _seed_journal_run(tmp_path, run_id=run_id, campaign_id="ml_q1_optuna")

        spec = ValidateStochasticMarkerSpec(
            campaign_id="ml_q1_optuna",
            expected_cmd_sha="aaaaaaa1234567890",
        )
        result = validate_stochastic_marker(tmp_path, spec=spec)
        assert len(result.findings) == 1
        assert len(result.matched_prior_run_ids) == 3
        # Newest-first ordering — the most-recent collision sorts first.
        assert result.matched_prior_run_ids == sorted(result.matched_prior_run_ids, reverse=True)
        assert result.findings[0].evidence["n_collisions"] == 3
