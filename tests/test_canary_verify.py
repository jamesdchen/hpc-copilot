"""Tests for ``claude_hpc.atoms.canary_verify``.

verify_canary is a workflow atom that wraps a polling SSH loop, so we
mock ``_ssh_status_report`` / ``fetch_task_logs`` /
``verify_combiner_artifact`` to drive the state machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal.session import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "home_hpc")
    return tmp_path / "home_hpc"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    """Skip time.sleep so the polling loop runs at memory speed."""
    monkeypatch.setattr("claude_hpc.atoms.canary_verify.time.sleep", lambda _: None)


def _seed_canary(experiment: Path, run_id: str = "r1-canary") -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/x",
        job_name="p_canary",
        job_ids=["job_42"],
        total_tasks=1,
        submitted_at="2026-01-01T00:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    session.upsert_run(experiment, record)
    return record


def test_happy_path_no_failure_markers(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.canary_verify import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "claude_hpc.runner.status._ssh_status_report",
            return_value={"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}},
        ),
        mock.patch(
            "claude_hpc.runner.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is True
    assert out["failure_kind"] is None


def test_dispatcher_failed_marker(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.canary_verify import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "claude_hpc.runner.status._ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "claude_hpc.runner.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] FAILED (exit 1)\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is False
    assert out["failure_kind"] == "dispatcher_failed"
    assert "[dispatch] FAILED" in out["stderr_tail"]


def test_traceback_marker(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.canary_verify import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "claude_hpc.runner.status._ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "claude_hpc.runner.fetch_task_logs",
            return_value=[
                {"task_id": 0, "content": 'Traceback (most recent call last):\n  File "..."\n'}
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["failure_kind"] == "traceback"


def test_import_error_more_specific_than_traceback(tmp_path: Path, journal_home: Path) -> None:
    """ImportError should be reported as import_error, not generic traceback."""
    from claude_hpc.atoms.canary_verify import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "claude_hpc.runner.status._ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "claude_hpc.runner.fetch_task_logs",
            return_value=[
                {
                    "task_id": 0,
                    "content": "Traceback (most recent call last):\nImportError: foo\n",
                }
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["failure_kind"] == "import_error"


def test_oom_killed(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.canary_verify import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "claude_hpc.runner.status._ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "claude_hpc.runner.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "Out of memory: kill process\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["failure_kind"] == "oom_killed"


def test_missing_output_when_expect_output_not_present(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.canary_verify import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "claude_hpc.runner.status._ssh_status_report",
            return_value={"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}},
        ),
        mock.patch(
            "claude_hpc.runner.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] OK\n"}],
        ),
        mock.patch(
            "claude_hpc.runner.aggregate.verify_combiner_artifact",
            return_value=(False, "is missing at /x/results/seed_42/metrics.json"),
        ),
    ):
        out = verify_canary(
            tmp_path,
            canary_run_id="r1-canary",
            expect_output="results/seed_42/metrics.json",
            wait_budget_sec=10,
        )
    assert out["ok"] is False
    assert out["failure_kind"] == "missing_output"


def test_no_journal_record_raises(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.canary_verify import verify_canary

    with pytest.raises(errors.SpecInvalid, match="no journal record"):
        verify_canary(tmp_path, canary_run_id="missing")


def test_empty_canary_run_id_raises(tmp_path: Path) -> None:
    from claude_hpc.atoms.canary_verify import verify_canary

    with pytest.raises(errors.SpecInvalid, match="canary_run_id"):
        verify_canary(tmp_path, canary_run_id="")
