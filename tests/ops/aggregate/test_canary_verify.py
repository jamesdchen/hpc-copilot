"""Tests for ``hpc_agent.ops.verify_canary``.

verify_canary is a workflow atom that wraps a polling SSH loop, so we
mock ``_ssh_status_report`` / ``fetch_task_logs`` /
``verify_combiner_artifact`` to drive the state machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    """Skip time.sleep so the polling loop runs at memory speed."""
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.sleep", lambda _: None)


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
    upsert_run(experiment, record)
    return record


def test_happy_path_no_failure_markers(tmp_path: Path, journal_home: Path) -> None:
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}},
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is True
    assert out["failure_kind"] is None


def test_dispatcher_failed_marker(tmp_path: Path, journal_home: Path) -> None:
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] FAILED (exit 1)\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is False
    assert out["failure_kind"] == "dispatcher_failed"
    assert "[dispatch] FAILED" in out["stderr_tail"]


def test_traceback_marker(tmp_path: Path, journal_home: Path) -> None:
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[
                {"task_id": 0, "content": 'Traceback (most recent call last):\n  File "..."\n'}
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["failure_kind"] == "traceback"


def test_import_error_more_specific_than_traceback(tmp_path: Path, journal_home: Path) -> None:
    """ImportError should be reported as import_error, not generic traceback."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
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
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "Out of memory: kill process\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["failure_kind"] == "oom_killed"


def test_missing_output_when_expect_output_not_present(tmp_path: Path, journal_home: Path) -> None:
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}},
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] OK\n"}],
        ),
        mock.patch(
            "hpc_agent.ops.aggregate.runner.verify_combiner_artifact",
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
    from hpc_agent.ops.verify_canary import verify_canary

    with pytest.raises(errors.SpecInvalid, match="no journal record"):
        verify_canary(tmp_path, canary_run_id="missing")


def test_empty_canary_run_id_raises(tmp_path: Path) -> None:
    from hpc_agent.ops.verify_canary import verify_canary

    with pytest.raises(errors.SpecInvalid, match="canary_run_id"):
        verify_canary(tmp_path, canary_run_id="")


def test_reporter_unreachable_when_every_poll_fails(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently-broken cluster-side reporter (every poll raises) must
    fail the canary as ``reporter_unreachable`` — not a silent pass, and not
    a misleading ``timeout`` — so the main array never submits against a
    cluster whose results can't be read (issue #135 item 4)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    # monotonic() runs once for the deadline, then per while-check. Cross the
    # deadline right after the first failed poll.
    ticks = iter([0.0, 1.0, 1e9, 1e9, 1e9])
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(ticks))
    with mock.patch(
        "hpc_agent.infra.cluster_status.ssh_status_report",
        side_effect=errors.RemoteCommandFailed(
            "status reporter failed (rc=1): /usr/bin/python: No module named ..."
        ),
    ):
        result = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=30)
    assert result["ok"] is False
    assert result["failure_kind"] == "reporter_unreachable"
    assert "reporter never returned" in result["details"]


def test_vanished_canary_resolves_completed_unknown_fast(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canary that finished/failed fast and left the scheduler queue shows an
    all-zero live summary. Once that persists across polls, verify-canary must
    resolve it as ``completed_unknown`` FAST (#193) — not ride the full wait
    budget to ``timeout``. The deadline is far away (1e9), so reaching a verdict
    proves the fast-path break, not a timeout."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    # monotonic() never crosses the deadline — if the loop didn't break on the
    # persistent all-zero summary it would spin forever (StopIteration), so a
    # clean verdict is the assertion that the fast break fired.
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: 0.0)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            # Job absent from the scheduler: every live bucket zero.
            return_value={
                "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
            },
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            # No stderr marker — the bland "left the queue" case.
            return_value=[{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["ok"] is False
    assert out["failure_kind"] == "completed_unknown"
    assert "left the scheduler queue" in out["details"]


def test_transient_all_zero_then_progress_does_not_false_trigger(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single all-zero poll right after qsub (scheduler hasn't registered the
    array yet) must NOT be read as vanished — the counter resets on the next
    poll that shows progress, and the canary completes normally (#193)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: 0.0)
    summaries = iter(
        [
            # poll 1: transient all-zero (pre-registration window)
            {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
            # poll 2: now pending — resets the vanished counter
            {"complete": 0, "running": 0, "pending": 1, "failed": 0, "unknown": 0},
            # poll 3: complete → normal terminal
            {"complete": 1, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
        ]
    )
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=lambda **_: {"summary": next(summaries)},
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["ok"] is True
    assert out["failure_kind"] is None


def test_vanished_canary_with_stderr_marker_prefers_the_marker(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the job vanished AND its stderr carries a real failure marker, the
    specific marker (oom_killed, traceback, ...) wins over the bland
    ``completed_unknown`` verdict — the scan runs after the fast break."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: 0.0)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={
                "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
            },
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "Out of memory: kill process\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["ok"] is False
    assert out["failure_kind"] == "oom_killed"


def test_passes_remote_activation_from_canary_sidecar(tmp_path: Path, journal_home: Path) -> None:
    """#176: the status reporter runs on the login node via ssh, so it needs the
    run's conda activation — otherwise it falls to bare login python, every poll
    raises, and the loop masks the real status as ``reporter_unreachable``.

    The activation is derived from the canary sidecar (cluster + resolved env),
    exactly like ``ops/monitor/status.py`` does for the normal status path.
    Before the fix the kwarg was omitted, so the reporter got ``""``.
    """
    from hpc_agent.infra.clusters import remote_activation_for_sidecar
    from hpc_agent.ops.verify_canary import verify_canary
    from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

    _seed_canary(tmp_path)  # journal record (cluster=hoffman2)
    # Canary sidecar carries cluster + resolved conda env (mirrored from main, #175).
    write_run_sidecar(
        tmp_path,
        run_id="r1-canary",
        cmd_sha="",
        hpc_agent_version="",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=1,
        tasks_py_sha="",
        cluster="hoffman2",
        env={"conda_env": "hpc-pi"},
    )
    captured: dict[str, object] = {}

    def _fake_status(**kwargs):
        captured["remote_activation"] = kwargs.get("remote_activation")
        return {"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}}

    with (
        mock.patch("hpc_agent.infra.cluster_status.ssh_status_report", side_effect=_fake_status),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] ok\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)

    assert out["ok"] is True
    expected = remote_activation_for_sidecar(read_run_sidecar(tmp_path, "r1-canary"))
    assert captured["remote_activation"] == expected
    assert captured["remote_activation"]  # non-empty — the #176 regression
    assert "conda activate hpc-pi" in captured["remote_activation"]
