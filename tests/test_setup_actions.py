"""Tests for ``claude_hpc.atoms.setup_actions``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal.session import RunRecord
from claude_hpc.atoms.setup_actions import find_prior_run, suggest_setup_action

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "home_hpc")
    return tmp_path / "home_hpc"


def _seed_journal(experiment: Path, run_id: str, **overrides) -> RunRecord:
    base = dict(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="user@h",
        remote_path="/x",
        job_name="p",
        job_ids=["job_42"],
        total_tasks=10,
        submitted_at="2026-01-01T00:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    base.update(overrides)
    record = RunRecord(**base)
    session.upsert_run(experiment, record)
    return record


def _seed_sidecar(experiment: Path, run_id: str, cmd_sha: str = "0" * 64) -> None:
    from claude_hpc.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha=cmd_sha,
        claude_hpc_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
        profile="ml_ridge",
        cluster="hoffman2",
    )


# ─── suggest-setup-action ──────────────────────────────────────────────────


def test_priority_3_fresh_when_nothing_exists(tmp_path: Path, journal_home: Path) -> None:
    out = suggest_setup_action(tmp_path)
    assert out["priority"] == 3
    assert out["action"] == "fresh"
    assert out["candidates"] == []


def test_priority_2_interview_when_only_tasks_py(tmp_path: Path, journal_home: Path) -> None:
    (tmp_path / ".hpc").mkdir()
    (tmp_path / ".hpc" / "tasks.py").write_text("def total(): return 1\n")
    out = suggest_setup_action(tmp_path)
    assert out["priority"] == 2
    assert out["action"] == "interview"


def test_priority_1_reuse_when_sidecars_exist(tmp_path: Path, journal_home: Path) -> None:
    _seed_sidecar(tmp_path, "20260101-000000-deadbee")
    out = suggest_setup_action(tmp_path)
    assert out["priority"] == 1
    assert out["action"] == "reuse"
    assert out["recommended_run_id"] == "20260101-000000-deadbee"
    assert out["candidates"][0]["profile"] == "ml_ridge"


def test_priority_0_monitor_when_in_flight(tmp_path: Path, journal_home: Path) -> None:
    _seed_journal(tmp_path, "running_run")
    _seed_sidecar(tmp_path, "running_run")  # priority 0 still wins
    out = suggest_setup_action(tmp_path)
    assert out["priority"] == 0
    assert out["action"] == "monitor"
    assert out["recommended_run_id"] == "running_run"
    assert out["candidates"][0]["job_ids"] == ["job_42"]


def test_priority_0_picks_newest_when_multiple_in_flight(
    tmp_path: Path, journal_home: Path
) -> None:
    _seed_journal(tmp_path, "older_run", submitted_at="2026-01-01T00:00:00+00:00")
    _seed_journal(tmp_path, "newer_run", submitted_at="2026-02-01T00:00:00+00:00")
    out = suggest_setup_action(tmp_path)
    assert out["priority"] == 0
    assert len(out["candidates"]) == 2
    # find_in_flight_runs returns newest-first; first candidate is the recommendation.
    assert out["recommended_run_id"] == out["candidates"][0]["run_id"]


# ─── find-prior-run ────────────────────────────────────────────────────────


def test_find_prior_run_no_match(tmp_path: Path, journal_home: Path) -> None:
    out = find_prior_run(tmp_path, cmd_sha="f" * 64)
    assert out["found"] is False
    assert out["prior_run_id"] is None
    assert out["job_ids"] == []


def test_find_prior_run_matches_sidecar(tmp_path: Path, journal_home: Path) -> None:
    cmd_sha = "a" * 64
    _seed_sidecar(tmp_path, "20260101-000000-deadbee", cmd_sha=cmd_sha)
    out = find_prior_run(tmp_path, cmd_sha=cmd_sha)
    assert out["found"] is True
    assert out["prior_run_id"] == "20260101-000000-deadbee"
    assert out["profile"] == "ml_ridge"
    assert out["cluster"] == "hoffman2"


def test_find_prior_run_marks_orphan_when_no_journal(tmp_path: Path, journal_home: Path) -> None:
    cmd_sha = "b" * 64
    _seed_sidecar(tmp_path, "20260101-000000-orphan01", cmd_sha=cmd_sha)
    out = find_prior_run(tmp_path, cmd_sha=cmd_sha)
    assert out["found"] is True
    assert out["is_orphan"] is True


def test_find_prior_run_not_orphan_when_journal_has_jobs(
    tmp_path: Path, journal_home: Path
) -> None:
    cmd_sha = "c" * 64
    _seed_sidecar(tmp_path, "real_run", cmd_sha=cmd_sha)
    _seed_journal(tmp_path, "real_run", job_ids=["job_99"])
    out = find_prior_run(tmp_path, cmd_sha=cmd_sha)
    assert out["found"] is True
    assert out["is_orphan"] is False


def test_find_prior_run_empty_cmd_sha_raises(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="cmd_sha"):
        find_prior_run(tmp_path, cmd_sha="")
