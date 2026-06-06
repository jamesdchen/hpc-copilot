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


# ── Fix 1: failure_features attached on every ok=False path ──────────────────


def test_failure_features_attached_on_dispatcher_failed(tmp_path: Path, journal_home: Path) -> None:
    """The empirical demo failure: dispatcher_failed today gives no cluster
    context. Fix 1 attaches `failure_features.cluster_log_tail` (the raw log
    tail under a structured key) AND `failure_features.classified_error`
    (the catalog match). For the bare `[dispatch] FAILED` marker no specific
    signature fires, so classified_error.error_class is "unknown" — the LOG
    TAIL is what the agent reads, and `_FAILURE_MARKERS` already routes it
    to dispatcher_failed."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    log_path = "/x/logs/p_canary.o42.1"
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
                    "content": "[dispatch] FAILED (exit 1)\n",
                    "path": log_path,
                    "job_id": "job_42",
                }
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is False
    assert out["failure_kind"] == "dispatcher_failed"
    feats = out["failure_features"]
    assert feats is not None
    assert "[dispatch] FAILED" in feats["cluster_log_tail"]
    assert feats["log_path"] == log_path
    classified = feats["classified_error"]
    assert classified is not None
    # No specific catalog row matches a bare dispatcher_failed marker, but the
    # classifier still ran (the proof Fix 1 wired it up).
    assert "error_class" in classified


def test_failure_features_classifies_uv_not_on_path(tmp_path: Path, journal_home: Path) -> None:
    """The most common 0.10.x cluster-side demo failure: ``HPC_RUNTIME=uv
    but 'uv' not on PATH``. classify() must surface ``uv_not_on_path``
    with the structured remediation, so the orchestrator sees an
    actionable fix rather than a generic dispatcher_failed."""
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
                    "content": (
                        "[dispatch] FAILED (exit 2)\n"
                        "[template] HPC_RUNTIME=uv but 'uv' not on PATH\n"
                    ),
                }
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is False
    feats = out["failure_features"]
    assert feats is not None
    classified = feats["classified_error"]
    assert classified["error_class"] == "uv_not_on_path"
    assert classified["suggested_fix"]["action"] == "drop-runtime-uv-or-install"


def test_failure_features_classifies_conda_command_not_found(
    tmp_path: Path, journal_home: Path
) -> None:
    """A bad ``conda_source`` in clusters.yaml surfaces as
    ``conda: command not found`` in the cluster log. The classifier
    must route this to ``conda_command_not_found``."""
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
                    "content": (
                        "[dispatch] FAILED (exit 127)\n"
                        "preamble.sh: line 12: conda: command not found\n"
                    ),
                }
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    feats = out["failure_features"]
    assert feats["classified_error"]["error_class"] == "conda_command_not_found"


def test_failure_features_classifies_module_not_found_hpc_agent(
    tmp_path: Path, journal_home: Path
) -> None:
    """Cluster-side python isn't the conda env's python — verifier routes
    to the hpc_agent-specific signature, not the generic import_error."""
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
                    "content": (
                        "Traceback (most recent call last):\n"
                        '  File "cli.py", line 1\n'
                        "ModuleNotFoundError: No module named 'hpc_agent'\n"
                    ),
                }
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    feats = out["failure_features"]
    assert feats["classified_error"]["error_class"] == "module_not_found_hpc_agent"


def test_failure_features_classifies_output_file_required(
    tmp_path: Path, journal_home: Path
) -> None:
    """Executor's argparse rejected its invocation — the framework's
    --output-file auto-inject didn't fire."""
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
                    "content": (
                        "executor.py: error: the following arguments are required: --output-file\n"
                    ),
                }
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    feats = out["failure_features"]
    assert feats["classified_error"]["error_class"] == "output_file_required"


def test_failure_features_classifies_undefined_var_expansion(
    tmp_path: Path, journal_home: Path
) -> None:
    """``--samples $SAMPLES`` with SAMPLES unexported → argparse sees an
    empty value and rejects with 'expected one argument'."""
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
                    "content": ("executor.py: error: argument --samples: expected one argument\n"),
                }
            ],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    feats = out["failure_features"]
    assert feats["classified_error"]["error_class"] == "undefined_var_expansion"


def test_failure_features_none_on_ok_canary(tmp_path: Path, journal_home: Path) -> None:
    """The success envelope intentionally omits ``failure_features`` —
    callers can use its non-None-ness as a "this is a failed canary"
    sentinel."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}},
        ),
        mock.patch(
            "hpc_agent.infra.cluster_logs.fetch_task_logs",
            return_value=[{"task_id": 0, "content": "[dispatch] ok\n"}],
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is True
    assert out["failure_features"] is None


def test_failure_features_on_timeout_and_reporter_unreachable(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-stderr-fetch failure paths (reporter_unreachable, timeout)
    still emit ``failure_features``, just with empty cluster_log_tail and
    classified_error=None — the structured shape is uniform across every
    ok=False path so the consumer never has to special-case 'no features
    here yet'."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    ticks = iter([0.0, 1.0, 1e9, 1e9, 1e9])
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(ticks))
    with mock.patch(
        "hpc_agent.infra.cluster_status.ssh_status_report",
        side_effect=errors.RemoteCommandFailed("reporter died"),
    ):
        result = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=30)
    assert result["ok"] is False
    assert result["failure_kind"] == "reporter_unreachable"
    feats = result["failure_features"]
    assert feats is not None
    assert feats["cluster_log_tail"] == ""
    assert feats["classified_error"] is None


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
