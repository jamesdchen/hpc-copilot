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
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    """Skip time.sleep so the polling loop runs at memory speed."""
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.sleep", lambda _: None)


@pytest.fixture(autouse=True)
def _light_poll_terminal(monkeypatch: pytest.MonkeyPatch):
    """Default the LIGHT liveness poll to 'announce census present, every task
    terminal' so the poll loop breaks terminal on the FIRST tick and the ONE heavy
    ``ssh_status_report`` (each test's own mock) supplies the AUTHORITATIVE summary
    the verdict rests on.

    2.2 liveness split: the poll loop no longer runs the full status reporter per
    tick — it issues only ``read_announcements`` (a bare ``ls`` census) and, before
    the dispatcher announces, one ``ssh_batch_scheduler_states`` (``qstat``). Those
    two light reads drive terminal detection; the heavy reporter runs exactly once
    at terminal. This fixture mocks both light reads so the verdict-logic tests
    (which mock ``ssh_status_report`` + ``_fused_verify_tail``) keep driving those
    seams verbatim. Tests that exercise multi-poll dynamics / the vanished path
    override these via their own ``monkeypatch``/``mock.patch``.
    """
    monkeypatch.setattr(
        "hpc_agent.ops.monitor.announce.read_announcements",
        lambda **_: {"present": 1, "announced": 1, "complete": 1, "failed": 0, "missing": 0},
    )
    monkeypatch.setattr(
        "hpc_agent.infra.cluster_status.ssh_batch_scheduler_states",
        lambda **_: {},
    )


def _census(*, present=1, complete=0, failed=0, missing=0):
    """A ``read_announcements`` return dict for driving the LIGHT poll in a test."""
    return {
        "present": present,
        "announced": complete + failed,
        "complete": complete,
        "failed": failed,
        "missing": missing,
    }


_VTAIL = "hpc_agent.ops.verify_canary._fused_verify_tail"


def _tail_from(logs, *, runtime=None, output=None, fingerprint_sha=None):
    """Build a fused-verify-tail bundle from the OLD ``fetch_task_logs`` shape.

    RANK 18 folded the post-terminal tail (stderr tail + _runtime.json + optional
    expect_output + optional sha) into ONE ssh (verify_canary's fused tail). These
    unit tests drive the verdict logic by mocking that ONE seam instead of the
    former per-read functions; this adapter maps a ``[{"task_id":0,"content":X}]``
    stderr stub (and optional runtime/output/fingerprint legs) to the bundle the
    verdict branches consume, so the existing stderr fixtures port verbatim.
    """
    content = ""
    log_path = None
    if logs and isinstance(logs[0], dict):
        content = str(logs[0].get("content") or "")
        log_path = logs[0].get("path")
    return {
        "stderr_tail": content,
        "log_path": log_path,
        "runtime": runtime,
        "output": output,
        "fingerprint_sha": fingerprint_sha,
    }


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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "[dispatch] FAILED (exit 1)\n"}]),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [{"task_id": 0, "content": 'Traceback (most recent call last):\n  File "..."\n'}]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [
                    {
                        "task_id": 0,
                        "content": "Traceback (most recent call last):\nImportError: foo\n",
                    }
                ]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "Out of memory: kill process\n"}]),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            # The expect_output existence check is now folded into the ONE fused
            # tail ssh — its verdict rides the bundle's ``output`` leg.
            return_value=_tail_from(
                [{"task_id": 0, "content": "[dispatch] OK\n"}],
                output=(False, "is missing at /x/results/r1-canary/seed_0/metrics.json"),
            ),
        ),
    ):
        out = verify_canary(
            tmp_path,
            canary_run_id="r1-canary",
            expect_output="results/r1-canary/seed_0/metrics.json",
            wait_budget_sec=10,
        )
    assert out["ok"] is False
    assert out["failure_kind"] == "missing_output"


def test_rejects_expect_output_not_referencing_canary_run_id(
    tmp_path: Path, journal_home: Path
) -> None:
    """A divined expect_output (main run_id / literal example seed) is refused at
    the boundary, not turned into a false missing_output for a passing canary."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="does not reference the canary run_id"):
        verify_canary(
            tmp_path,
            canary_run_id="r1-canary",
            expect_output="results/r1/seed_42/metrics.json",
            wait_budget_sec=10,
        )


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


def test_open_circuit_rides_budget_to_reporter_unreachable(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``SshCircuitOpen`` (an HpcError, NOT an OSError) tripped by a transient
    breaker must be classified transient and ride the wait budget to
    ``reporter_unreachable`` — never crash the canary gate with an undeclared
    exception (bug-sweep #50)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    ticks = iter([0.0, 1.0, 1e9, 1e9, 1e9])
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(ticks))
    with mock.patch(
        "hpc_agent.infra.cluster_status.ssh_status_report",
        side_effect=errors.SshCircuitOpen("breaker open for user@h until deadline"),
    ):
        result = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=30)
    assert result["ok"] is False
    assert result["failure_kind"] == "reporter_unreachable"


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
    # monotonic() advances 100s per read (far above the default 30s registration
    # grace, well under the 1800s deadline) so the all-zero state spans the grace
    # by the 2nd poll and the fast break fires — proving the verdict, not a timeout.
    import itertools

    _clk = itertools.count(0.0, 100.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
    with (
        # 2.2: no announce dir + job gone from the scheduler ⇒ the light poll is
        # all-zero, so the vanished-grace arm fires.
        mock.patch(
            "hpc_agent.ops.monitor.announce.read_announcements",
            return_value=_census(present=0),
        ),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            # Job absent from the scheduler: every live bucket zero.
            return_value={
                "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
            },
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            # No stderr marker — the bland "left the queue" case.
            return_value=_tail_from(
                [{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}]
            ),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["ok"] is False
    assert out["failure_kind"] == "completed_unknown"
    assert "left the scheduler queue" in out["details"]


def test_vanished_canary_bucketed_unknown_resolves_completed_unknown_fast(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-L (run #10): tonight's qacct evidence — canary job 13956468 was dead on
    the scheduler in ~19s (single attempt, exit_status 1), yet verify_canary
    burned the full 1800s budget and returned ``timeout``. The mechanism: the
    status reporter buckets a 1-task job that left the queue with NO result file
    as ``unknown == 1`` (a task that is neither complete, in a scheduler-FAILED
    accounting state, nor live in qstat lands in ``unknown``) — NOT the all-zero
    ``unknown == 0`` the fast-arm's guard required. So the completed_unknown arm
    never fired. This reproduces the REAL reporter summary (unknown=1); the
    verdict must arrive fast as ``completed_unknown``, NEVER ``timeout``."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    # Clock advances 100s per read (spans the 30s registration grace by the 2nd
    # poll, far under the 1800s deadline) so reaching a verdict proves the fast
    # break, not a timeout.
    import itertools

    _clk = itertools.count(0.0, 100.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
    with (
        mock.patch(
            "hpc_agent.ops.monitor.announce.read_announcements",
            return_value=_census(present=0),  # 2.2: light poll → vanished-grace arm
        ),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            # The REAL reporter output for a gone 1-task canary: the vanished
            # task sits in the ``unknown`` bucket, not an all-zero summary.
            return_value={
                "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 1}
            },
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            # No stderr marker / no completion artifact — the bland "left the
            # queue too fast to observe" case.
            return_value=_tail_from(
                [{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}]
            ),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["ok"] is False
    assert out["failure_kind"] == "completed_unknown", out
    assert "left the scheduler queue" in out["details"]


def test_transient_unknown_then_progress_does_not_false_trigger(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup-window counterpart to the F-L fix: a fresh canary not yet
    registered in qstat ALSO reads ``unknown == 1`` (same bucket as a gone job).
    A single such poll must NOT be read as vanished — the counter resets the
    moment a task shows running/pending, and the canary completes normally. The
    registration grace, not the bucket value, distinguishes startup from gone."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    import itertools

    _clk = itertools.count(0.0, 100.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
    summaries = iter(
        [
            # poll 1: pre-registration — the task is bucketed unknown
            {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 1},
            # poll 2: now running — resets the vanished counter
            {"complete": 0, "running": 1, "pending": 0, "failed": 0, "unknown": 0},
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}]
            ),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["ok"] is True
    assert out["failure_kind"] is None


def test_transient_all_zero_then_progress_does_not_false_trigger(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single all-zero poll right after qsub (scheduler hasn't registered the
    array yet) must NOT be read as vanished — the counter resets on the next
    poll that shows progress, and the canary completes normally (#193)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    import itertools

    _clk = itertools.count(0.0, 100.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}]
            ),
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
    import itertools

    _clk = itertools.count(0.0, 100.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={
                "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
            },
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "Out of memory: kill process\n"}]),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [
                    {
                        "task_id": 0,
                        "content": "[dispatch] FAILED (exit 1)\n",
                        "path": log_path,
                        "job_id": "job_42",
                    }
                ]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [
                    {
                        "task_id": 0,
                        "content": (
                            "[dispatch] FAILED (exit 2)\n"
                            "[template] HPC_RUNTIME=uv but 'uv' not on PATH\n"
                        ),
                    }
                ]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [
                    {
                        "task_id": 0,
                        "content": (
                            "[dispatch] FAILED (exit 127)\n"
                            "preamble.sh: line 12: conda: command not found\n"
                        ),
                    }
                ]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [
                    {
                        "task_id": 0,
                        "content": (
                            "Traceback (most recent call last):\n"
                            '  File "cli.py", line 1\n'
                            "ModuleNotFoundError: No module named 'hpc_agent'\n"
                        ),
                    }
                ]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [
                    {
                        "task_id": 0,
                        "content": (
                            "executor.py: error: the following arguments are "
                            "required: --output-file\n"
                        ),
                    }
                ]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [
                    {
                        "task_id": 0,
                        "content": (
                            "executor.py: error: argument --samples: expected one argument\n"
                        ),
                    }
                ]
            ),
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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "[dispatch] ok\n"}]),
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


# ── #294 PR4: checkpoint-canary round-trip verification ──────────────────────


def _fake_ssh_completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """A subprocess.CompletedProcess stand-in for a mocked ssh_run."""
    import subprocess

    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _seed_canary_with_sidecar(
    experiment: Path, *, result_dir_template: str = "results/{run_id}/task_{task_id}"
) -> None:
    """Journal record + canary sidecar (so the checkpoint dir can be derived)."""
    from hpc_agent.state.runs import write_run_sidecar

    _seed_canary(experiment)
    write_run_sidecar(
        experiment,
        run_id="r1-canary",
        cmd_sha="",
        hpc_agent_version="",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template=result_dir_template,
        task_count=1,
        tasks_py_sha="",
        cluster="hoffman2",
    )


# A canary that was preempted (exit 130) shows failed=1 / complete=0 — that is
# the EXPECTED terminal state for a checkpoint canary, so the poll loop breaks
# on it and the checkpoint branch (not the exit-0 path) decides the verdict.
_PREEMPTED_SUMMARY = {"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}}
_PREEMPT_STDERR = [
    {
        "task_id": 0,
        "content": (
            "[hpc-agent] SIGTERM received; cluster preemption imminent\n"
            "[dispatch] FAILED (exit 130), partial output preserved in ...\n"
        ),
    }
]


def test_checkpoint_canary_ok_when_loadable(tmp_path: Path, journal_home: Path) -> None:
    """A loadable checkpoint that survived the kill → ok=True, even though the
    canary exited 130 with a '[dispatch] FAILED' marker (which the non-checkpoint
    path would have flagged as dispatcher_failed)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar(tmp_path)
    probe_out = (
        '{"status": "ok", "path": "/x/results/r1-canary/task_0/_checkpoints/'
        'checkpoint-0.pkl", "next_iteration": 1}'
    )
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run", return_value=_fake_ssh_completed(stdout=probe_out)
        ) as m_ssh,
    ):
        out = verify_canary(
            tmp_path, canary_run_id="r1-canary", verify_checkpoint=True, wait_budget_sec=10
        )
    assert out["ok"] is True
    assert out["failure_kind"] is None
    assert "resumes at iteration 1" in out["details"]
    assert out["failure_features"] is None
    # The probe ran against the derived task-0 checkpoint dir.
    cmd = m_ssh.call_args.args[0]
    assert "results/r1-canary/task_0" in cmd


def test_checkpoint_canary_missing_fails_gate(tmp_path: Path, journal_home: Path) -> None:
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run",
            return_value=_fake_ssh_completed(stdout='{"status": "missing"}'),
        ),
    ):
        out = verify_canary(
            tmp_path, canary_run_id="r1-canary", verify_checkpoint=True, wait_budget_sec=10
        )
    assert out["ok"] is False
    assert out["failure_kind"] == "checkpoint_missing"
    # Stderr is classified into failure_features so the operator gets the cause.
    assert out["failure_features"] is not None


def test_checkpoint_canary_unloadable_fails_gate(tmp_path: Path, journal_home: Path) -> None:
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar(tmp_path)
    probe = (
        '{"status": "unloadable", '
        '"path": "/x/results/r1-canary/task_0/_checkpoints/checkpoint-0.pkl"}'
    )
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run", return_value=_fake_ssh_completed(stdout=probe)
        ),
    ):
        out = verify_canary(
            tmp_path, canary_run_id="r1-canary", verify_checkpoint=True, wait_budget_sec=10
        )
    assert out["ok"] is False
    assert out["failure_kind"] == "checkpoint_unloadable"
    assert "does not round-trip" in out["details"]


def test_checkpoint_canary_probe_failure_is_reporter_unreachable(
    tmp_path: Path, journal_home: Path
) -> None:
    """An ssh error / non-zero remote probe can't confirm the round-trip — fail
    loudly (reporter_unreachable) rather than silently pass."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run",
            return_value=_fake_ssh_completed(returncode=1, stderr="No module named 'hpc_agent'"),
        ),
    ):
        out = verify_canary(
            tmp_path, canary_run_id="r1-canary", verify_checkpoint=True, wait_budget_sec=10
        )
    assert out["ok"] is False
    assert out["failure_kind"] == "reporter_unreachable"


def test_checkpoint_canary_explicit_result_dir_override(tmp_path: Path, journal_home: Path) -> None:
    """An explicit checkpoint_result_dir is used verbatim (no sidecar template
    derivation needed)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)  # journal record only, NO sidecar template
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run",
            return_value=_fake_ssh_completed(
                stdout=(
                    '{"status": "ok", "path": "/x/custom/dir/_checkpoints/'
                    'checkpoint-0.pkl", "next_iteration": 1}'
                )
            ),
        ) as m_ssh,
    ):
        out = verify_canary(
            tmp_path,
            canary_run_id="r1-canary",
            verify_checkpoint=True,
            checkpoint_result_dir="custom/dir",
            wait_budget_sec=10,
        )
    assert out["ok"] is True
    assert "custom/dir" in m_ssh.call_args.args[0]


def test_checkpoint_canary_unrenderable_template_raises(tmp_path: Path, journal_home: Path) -> None:
    """A result_dir_template that references a per-task kwarg can't be rendered
    locally → SpecInvalid asking for an explicit checkpoint_result_dir."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar(tmp_path, result_dir_template="results/seed_{seed}")
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
        pytest.raises(errors.SpecInvalid, match="checkpoint_result_dir"),
    ):
        verify_canary(
            tmp_path, canary_run_id="r1-canary", verify_checkpoint=True, wait_budget_sec=10
        )


def test_checkpoint_mode_off_keeps_normal_marker_scan(tmp_path: Path, journal_home: Path) -> None:
    """verify_checkpoint defaults False — a '[dispatch] FAILED' canary still
    routes to dispatcher_failed via the normal marker scan (no behavior change
    for non-checkpoint runs)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is False
    assert out["failure_kind"] == "dispatcher_failed"


def test_remote_checkpoint_snippet_logic(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the REMOTE snippet (executed as a string on the cluster) in-process
    so its missing/ok/unloadable branches + the throwaway-checkpoint cleanup are
    actually covered — otherwise they only fail on a real cluster."""
    import json
    import sys

    from hpc_agent.experiment_kit import checkpoint as ck
    from hpc_agent.ops.verify_canary import _REMOTE_CHECKPOINT_SNIPPET

    code = compile(_REMOTE_CHECKPOINT_SNIPPET, "<snippet>", "exec")
    d = tmp_path / "rd"

    def _run() -> dict:
        monkeypatch.setattr(sys, "argv", ["-c", str(d)])
        exec(code, {})  # noqa: S102 — exercising the remote snippet's own logic
        parsed: dict = json.loads(capsys.readouterr().out.strip())
        return parsed

    # No checkpoint yet → missing.
    assert _run()["status"] == "missing"

    # A loadable checkpoint → ok, resumes at iteration 1, and the dir is cleaned up.
    ck.write_checkpoint({"w": [1, 2]}, iteration=0, result_dir=d)
    out = _run()
    assert out["status"] == "ok"
    assert out["next_iteration"] == 1
    assert not (d / "_checkpoints").exists()  # throwaway probe cleaned up

    # A present-but-corrupt checkpoint → unloadable (distinct from missing).
    ckdir = d / "_checkpoints"
    ckdir.mkdir(parents=True)
    (ckdir / "checkpoint-0.pkl").write_bytes(b"\x80\x05 not a pickle")
    assert _run()["status"] == "unloadable"


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
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "[dispatch] ok\n"}]),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)

    assert out["ok"] is True
    expected = remote_activation_for_sidecar(read_run_sidecar(tmp_path, "r1-canary"))
    assert captured["remote_activation"] == expected
    assert captured["remote_activation"]  # non-empty — the #176 regression
    assert "conda activate hpc-pi" in captured["remote_activation"]


def test_activation_derived_from_record_cluster_when_sidecar_bare(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run #7 live regression: the canary sidecar this flow writes carries
    NEITHER ``env`` NOR ``cluster``, so the #176 derivation fell through to
    ``""`` → bare login-node python → ``No module named hpc_agent`` → rc=1
    every poll, riding the full wait budget against a green canary. The
    journal record always knows the cluster — verify_canary seeds it into the
    sidecar dict so the deriver's cluster-backfill arm (#281) fires."""
    from hpc_agent.ops.verify_canary import verify_canary
    from hpc_agent.state.runs import write_run_sidecar

    # Hermetic cluster config: the backfill arm reads clusters.yaml[hoffman2],
    # so pin one with a resolvable conda env — the machine's real config (or
    # the packaged placeholder on CI, which has no conda_envs) must not leak in.
    clusters = tmp_path / "clusters_fixture.yaml"
    clusters.write_text(
        "hoffman2:\n"
        "  host: h.example\n"
        "  user: u\n"
        "  scratch: /s\n"
        "  scheduler: sge\n"
        "  conda_source: /apps/conda/etc/profile.d/conda.sh\n"
        "  conda_envs: [hpc-pi]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))

    _seed_canary(tmp_path)  # journal record (cluster=hoffman2)
    # The bare shape actually written live (run #7): no cluster=, no env=.
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
    )
    captured: dict[str, object] = {}

    def _fake_status(**kwargs):
        captured["remote_activation"] = kwargs.get("remote_activation")
        return {"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}}

    with (
        mock.patch("hpc_agent.infra.cluster_status.ssh_status_report", side_effect=_fake_status),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "[dispatch] ok\n"}]),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)

    assert out["ok"] is True
    # Cluster-derived activation, not the bare-python "" fallthrough.
    assert captured["remote_activation"]
    assert "conda activate hpc-pi" in str(captured["remote_activation"])


def test_checkpoint_canary_petsc_structural_ok(tmp_path: Path, journal_home: Path) -> None:
    """A petsc_binary artifact verified structurally → ok=True, with the
    format and proof level surfaced (no next_iteration claim is made — the
    probe did not reload the Vec)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar(tmp_path)
    probe_out = (
        '{"status": "ok", "path": "/x/results/r1-canary/task_0/_checkpoints/'
        'petsc-solution.bin", "format": "petsc_binary", "level": "structural", '
        '"detail": "1 complete Vec block(s), 8-byte scalars, no trailing garbage"}'
    )
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run", return_value=_fake_ssh_completed(stdout=probe_out)
        ),
    ):
        out = verify_canary(
            tmp_path, canary_run_id="r1-canary", verify_checkpoint=True, wait_budget_sec=10
        )
    assert out["ok"] is True
    assert out["failure_kind"] is None
    assert "petsc_binary" in out["details"]
    assert "structural" in out["details"]
    assert "resumes at iteration" not in out["details"]


def test_checkpoint_canary_petsc_unloadable_names_format(
    tmp_path: Path, journal_home: Path
) -> None:
    """A garbage petsc artifact fails the gate with the format + the
    structural reason in the details."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar(tmp_path)
    probe_out = (
        '{"status": "unloadable", "path": "/x/results/r1-canary/task_0/_checkpoints/'
        'checkpoint-0.petscbin", "format": "petsc_binary", "level": "structural", '
        '"detail": "no PETSc Vec block found"}'
    )
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_PREEMPTED_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_PREEMPT_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run", return_value=_fake_ssh_completed(stdout=probe_out)
        ),
    ):
        out = verify_canary(
            tmp_path, canary_run_id="r1-canary", verify_checkpoint=True, wait_budget_sec=10
        )
    assert out["ok"] is False
    assert out["failure_kind"] == "checkpoint_unloadable"
    assert "petsc_binary" in out["details"]
    assert "no PETSc Vec block found" in out["details"]


# ── #351-3: positive exit_code read from _runtime.json ───────────────────────


def _seed_canary_with_sidecar_cmd_sha(
    experiment: Path,
    *,
    cmd_sha: str = "deadbeef",
    result_dir_template: str = "results/{run_id}/task_{task_id}",
) -> None:
    """Journal record + canary sidecar carrying a real cmd_sha.

    A non-empty cmd_sha is what a canary carries into the #249 TTL cache. Since
    B7 the cache is minted by ``submit_and_verify`` AFTER the full gate, never by
    ``verify_canary`` itself — so a bare ``verify_canary`` call (below) verifies
    the canary WITHOUT touching the cache. These tests still assert a failing
    canary is never cached (the mint can only be reached past ``ok=True``).
    """
    from hpc_agent.state.runs import write_run_sidecar

    _seed_canary(experiment)
    write_run_sidecar(
        experiment,
        run_id="r1-canary",
        cmd_sha=cmd_sha,
        hpc_agent_version="",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template=result_dir_template,
        task_count=1,
        tasks_py_sha="",
        cluster="hoffman2",
    )


def _is_cmd_sha_cached(cmd_sha: str, *, cluster: str = "hoffman2") -> bool:
    """Whether ``cmd_sha`` was recorded canary-validated (the cache poisoning).

    ``cluster`` defaults to the seeded canary's cluster (``hoffman2``) — the key
    joined cluster in proving run #5, and ``record_canary_validated`` keys on the
    run's own cluster, so the poisoning check must read the SAME triple.
    """
    from hpc_agent import __version__ as pkg_version
    from hpc_agent.state import canary_cache

    return canary_cache.is_canary_validated_fresh(
        canary_cache.canary_cache_key(cmd_sha=cmd_sha, version=pkg_version or "", cluster=cluster)
    )


# A canary that wrote a (partial) result file and went "complete" in the
# scheduler's view, yet whose dispatcher recorded a non-zero exit — the exact
# #351-3 trap: scheduler-state + result-presence + a clean 50-line stderr tail
# all "pass", but the task actually failed.
_COMPLETE_SUMMARY = {"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}}
_CLEAN_STDERR = [{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1-canary\n"}]


def test_nonzero_exit_in_runtime_json_fails_gate_and_is_not_cached(
    tmp_path: Path, journal_home: Path
) -> None:
    """#351-3: a canary reported 'complete' with a clean stderr tail, but its
    task-0 ``_runtime.json`` recorded ``exit_code: 1`` (e.g. a TypeError whose
    traceback fell outside the fetched 50 lines). verify_canary must read that
    exit code over SSH, return ``ok=False`` + ``failure_kind="nonzero_exit"``,
    AND never cache the failing cmd_sha (no cache poisoning)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar_cmd_sha(tmp_path, cmd_sha="sha_fails")
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_COMPLETE_SUMMARY
        ),
        # The _runtime.json exit_code now rides the ONE fused tail ssh (RANK 18);
        # its 'present' verdict is the bundle's ``runtime`` leg.
        mock.patch(
            _VTAIL,
            return_value=_tail_from(_CLEAN_STDERR, runtime={"status": "present", "exit_code": 1}),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is False
    assert out["failure_kind"] == "nonzero_exit"
    assert "exit_code=1" in out["details"]
    assert "_runtime.json" in out["details"]
    # failure_features carries the stderr tail for the operator.
    assert out["failure_features"] is not None
    # The poisoning check: a FAILING cmd_sha must NOT be cached as validated.
    assert _is_cmd_sha_cached("sha_fails") is False


def test_zero_exit_in_runtime_json_passes_and_claims_exit_0(
    tmp_path: Path, journal_home: Path
) -> None:
    """The positive side of the guard: when ``_runtime.json`` records
    ``exit_code: 0``, the canary passes AND the details string truthfully
    claims 'exit 0' (it was actually read, not asserted blindly).

    B7: ``verify_canary`` itself does NOT mint the #249 cache — a single verify
    is one leg of a canary PAIR, and minting mid-gate let a failed second canary
    ride a poisoned cache. The mint moved to ``submit_and_verify`` past both
    verdicts, so a bare ``verify_canary`` success leaves the cache untouched."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar_cmd_sha(tmp_path, cmd_sha="sha_ok")
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_COMPLETE_SUMMARY
        ),
        mock.patch(
            _VTAIL,
            return_value=_tail_from(_CLEAN_STDERR, runtime={"status": "present", "exit_code": 0}),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is True
    assert out["failure_kind"] is None
    assert "exit 0" in out["details"]
    assert out["failure_features"] is None
    # B7: minting is the gate's job (submit_and_verify), not a single verify's —
    # a bare verify_canary success does NOT stamp the #249 skip cache.
    assert _is_cmd_sha_cached("sha_ok") is False


def test_absent_runtime_json_falls_through_to_existing_logic(
    tmp_path: Path, journal_home: Path
) -> None:
    """#351-3 is ADDITIVE: an ABSENT ``_runtime.json`` (a preamble crash before
    the dispatcher writes it) must NOT mint a false failure — it falls through
    to the unchanged success logic, and the details string does NOT lie 'exit 0'
    since the exit code was never read."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar_cmd_sha(tmp_path, cmd_sha="sha_absent")
    # `cat ... || echo __HPC_NO_RUNTIME__` → the sentinel for a missing file.
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_COMPLETE_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_CLEAN_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run",
            return_value=_fake_ssh_completed(stdout="__HPC_NO_RUNTIME__"),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is True
    assert out["failure_kind"] is None
    # The exit code was NEVER read → the details string must not assert "exit 0".
    assert "exit 0" not in out["details"]
    assert "no error markers" in out["details"]


def test_unreadable_runtime_json_falls_through_does_not_fail_canary(
    tmp_path: Path, journal_home: Path
) -> None:
    """A non-JSON / unreadable ``_runtime.json`` (ssh hiccup, truncated write)
    must NOT mint a false ``nonzero_exit`` — we never fail a canary from a read
    miss; the existing stderr / failed-count paths already gate real crashes."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar_cmd_sha(tmp_path, cmd_sha="sha_garbled")
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report", return_value=_COMPLETE_SUMMARY
        ),
        mock.patch(_VTAIL, return_value=_tail_from(_CLEAN_STDERR)),
        mock.patch(
            "hpc_agent.infra.remote.ssh_run",
            return_value=_fake_ssh_completed(stdout="not json {{{"),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is True
    assert out["failure_kind"] is None
    assert "exit 0" not in out["details"]


def test_nonzero_exit_guard_runs_after_existing_failure_paths(
    tmp_path: Path, journal_home: Path
) -> None:
    """The exit_code read is ADDITIVE: a stderr failure marker still wins (the
    guard runs only after the marker scan, which short-circuits first). Proven by
    a [dispatch] FAILED marker resolving to dispatcher_failed even though a
    _runtime.json would be available — ssh_run is never reached for the read."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary_with_sidecar_cmd_sha(tmp_path, cmd_sha="sha_marker")
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 1}},
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "[dispatch] FAILED (exit 1)\n"}]),
        ),
        mock.patch("hpc_agent.infra.remote.ssh_run") as m_ssh,
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is False
    assert out["failure_kind"] == "dispatcher_failed"
    # The stderr scan short-circuited before the exit_code read ran.
    m_ssh.assert_not_called()
    assert _is_cmd_sha_cached("sha_marker") is False


# --- Adaptive fast-start (canary poll latency) -------------------------------
#
# The canary is a 1-task probe on the critical path of every fresh submit, so
# the poll loop ramps from a small fast-start floor toward poll_interval_sec
# (the steady-state ceiling) instead of dead-waiting a flat interval. See
# ``_CANARY_FAST_POLL_SEC`` / ``_next_poll_interval`` in verify_canary.


def test_initial_poll_interval_uses_fast_start_floor() -> None:
    from hpc_agent.ops import verify_canary as vc

    # Default floor (3s) wins when it's below the configured interval...
    assert vc._initial_poll_interval(30) == 3.0
    # ...but a caller asking for a FASTER steady cadence than the floor is
    # honored as-is — the floor never slows the loop down.
    assert vc._initial_poll_interval(1) == 1.0


def test_initial_poll_interval_opt_out_falls_back_to_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hpc_agent.ops import verify_canary as vc

    # HPC_CANARY_FAST_POLL_SEC=0 opts out → start at the configured interval.
    monkeypatch.setattr(vc, "_CANARY_FAST_POLL_SEC", 0.0)
    assert vc._initial_poll_interval(30) == 30.0


def test_next_poll_interval_doubles_and_caps_at_ceiling() -> None:
    from hpc_agent.ops import verify_canary as vc

    assert vc._next_poll_interval(3.0, 30.0) == 6.0
    assert vc._next_poll_interval(6.0, 30.0) == 12.0
    assert vc._next_poll_interval(12.0, 30.0) == 24.0
    # Past the ceiling, hold the configured cadence — never poll slower.
    assert vc._next_poll_interval(24.0, 30.0) == 30.0
    assert vc._next_poll_interval(30.0, 30.0) == 30.0


def test_fast_start_allzero_within_grace_is_not_falsely_vanished(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the fast-start ramp must NOT trip the 2-consecutive-all-zero
    vanished verdict before the scheduler has had ``poll_interval_sec`` to list
    the array. Two rapid all-zero polls (a slow-to-register canary) followed by
    progress must complete normally, not fail as ``completed_unknown``."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    # Clock advances only 2s per read: two all-zero polls land well inside the
    # 30s registration grace. Without the time floor the 2nd poll would falsely
    # declare the canary vanished; with it, the verdict waits for the grace.
    _clk = itertools.count(0.0, 2.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
    summaries = iter(
        [
            {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0},  # all-zero
            {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0},  # all-zero
            {"complete": 0, "running": 1, "pending": 0, "failed": 0, "unknown": 0},  # registered!
            {"complete": 1, "running": 0, "pending": 0, "failed": 0, "unknown": 0},  # complete
        ]
    )
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=lambda **_: {"summary": next(summaries)},
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}]
            ),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["ok"] is True, out
    assert out["failure_kind"] is None


def test_poll_loop_ramps_instead_of_flat_waiting(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canary that stays in-flight for several polls is observed on the
    fast-start ramp (3 → 6 → 12 …), not after flat 30s intervals."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)

    # monotonic advances 1s per read; a large budget keeps us under the deadline
    # so the loop exits on the COMPLETE poll, not on timeout.
    _clock = itertools.count(0, 1.0)
    monkeypatch.setattr(
        "hpc_agent.ops.verify_canary.time.monotonic",
        lambda: next(_clock),
    )
    sleeps: list[float] = []
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.sleep", sleeps.append)

    # 2.2: the RAMP is driven by the LIGHT poll (read_announcements). Three
    # in-flight census reads (a task still unannounced ⇒ pending), then a
    # complete census on the fourth breaks terminal; the ONE heavy reporter runs
    # once at terminal.
    in_flight = _census(present=1, complete=0, missing=1)  # → pending=1, keep polling
    done_census = _census(present=1, complete=1, missing=0)  # → COMPLETE, break
    done = {"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}}
    with (
        mock.patch(
            "hpc_agent.ops.monitor.announce.read_announcements",
            side_effect=[in_flight, in_flight, in_flight, done_census],
        ),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value=done,
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "[dispatch] task_id=0\n"}]),
        ),
    ):
        out = verify_canary(
            tmp_path, canary_run_id="r1-canary", poll_interval_sec=30, wait_budget_sec=100_000
        )

    assert out["ok"] is True
    # Three sleeps between four polls, ramping from the 3s floor — NOT flat 30s.
    assert sleeps == [3.0, 6.0, 12.0]


# ── Finding 12/7: poll-failure-class escalation at the ONE terminal reporter ───
#
# 2.2 liveness split: the LIGHT poll (an ``ls`` census / one ``qstat``) never runs
# the run's python, so a broken conda env (rc 127) / a torn-or-never-shipped
# sidecar reporter fault can no longer stream across many polls — it surfaces at
# the ONE terminal status reporter, which escalates loud+fast (never a full 30-min
# budget spin), reusing the finding-12/7 envelopes. The escalation reads the
# env-independent ``.hpc_failed`` markers with plain sh: present → positive
# ``canary_failed``; absent → still a loud ``reporter_unreachable`` (the scan
# proves FAILURE only — a marker-less blind run is never called passed). A
# TRANSIENT terminal fault is a plain ``reporter_unreachable`` (no escalation
# annotation, no marker scan) — that class belongs to the connection breaker.


def _rc127() -> errors.RemoteCommandFailed:
    """The broken-env poll failure: reporter died rc 127 (command not found)."""
    return errors.RemoteCommandFailed(
        "status reporter failed (rc=127): /usr/bin/python: command not found",
        returncode=127,
    )


def test_deterministic_env_rc127_escalates_early_with_marker(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2.2: the ONE terminal reporter hits rc 127 → escalate (never a full-budget
    spin); the plain-sh marker scan finds a ``.hpc_failed`` marker → positive
    ``canary_failed`` with the marker name + rc as evidence, scan invoked once."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    _clk = itertools.count(0.0, 1.0)  # far under the 1800s deadline → escalation, not timeout
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))

    scan_calls: list[dict] = []

    def _fake_scan(**kw):
        scan_calls.append(kw)
        return {"failed_markers": ["r1-canary.0.failed"], "count": 1}

    with (
        mock.patch("hpc_agent.infra.cluster_status.ssh_status_report", side_effect=_rc127()),
        mock.patch("hpc_agent.infra.cluster_status.ssh_marker_scan", side_effect=_fake_scan),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)

    assert out["ok"] is False
    assert out["failure_kind"] == "canary_failed"
    assert "r1-canary.0.failed" in out["details"]
    assert "rc=127" in out["details"]
    # Escalated on the 3rd deterministic poll — the marker scan ran exactly once.
    assert len(scan_calls) == 1
    assert scan_calls[0]["run_id"] == "r1-canary"


def test_deterministic_env_rc127_escalates_early_without_marker(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same terminal-reporter rc-127 escalation, but NO ``.hpc_failed`` marker
    exists → the scan can only prove failure, never success, so the verdict is a
    loud ``reporter_unreachable`` (never a pass) annotated with the escalation
    evidence — the never-pass-unverified posture."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    _clk = itertools.count(0.0, 1.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))

    with (
        mock.patch("hpc_agent.infra.cluster_status.ssh_status_report", side_effect=_rc127()),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_marker_scan",
            return_value={"failed_markers": [], "count": 0},
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)

    assert out["ok"] is False
    assert out["failure_kind"] == "reporter_unreachable"
    # 2.2: the escalation fires at the ONE terminal reporter (rc 127), not a
    # per-poll streak — the light poll never runs the run's python.
    assert "terminal status reporter failed rc=127" in out["details"]
    assert "no .hpc_failed marker" in out["details"]


def test_marker_scan_ssh_failure_does_not_yield_pass(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the marker scan itself can't run (SSH transport down), the never-
    pass arm holds: the verdict is ``reporter_unreachable`` (ok=False), never a
    silent pass of an unverified run."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    _clk = itertools.count(0.0, 1.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))

    with (
        mock.patch("hpc_agent.infra.cluster_status.ssh_status_report", side_effect=_rc127()),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_marker_scan",
            side_effect=errors.RemoteCommandFailed("marker scan ssh failed"),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)

    assert out["ok"] is False
    assert out["failure_kind"] == "reporter_unreachable"


def test_fused_verify_tail_reads_all_legs_in_one_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """RANK 18: the post-terminal tail (stderr + _runtime.json + expect_output +
    sha) is read in ONE sentinel-sectioned ssh and parsed into the same fields the
    verdict branches consume. One ssh_run call; every section parsed."""
    from hpc_agent.ops.verify_canary import _fused_verify_tail

    calls: list[str] = []
    fused_out = (
        "<<<HPC_VTAIL:TAIL>>>\n"
        "FOUND\t/x/logs/p_canary.o42.1\n"
        "line-one\nline-two\n"
        "<<<HPC_VTAIL:RUNTIME>>>\n"
        '{"task_id": 0, "exit_code": 0}\n'
        "<<<HPC_VTAIL:OUTPUT>>>\n"
        "OK\n"
        "<<<HPC_VTAIL:FPRINT>>>\n"
        "deadbeefcafe\n"
        "__HPC_VTAIL_ACK__=0\n"
    )

    def _fake_ssh_run(cmd, *, ssh_target):  # noqa: ANN001
        calls.append(cmd)
        return _fake_ssh_completed(stdout=fused_out)

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _fake_ssh_run)
    bundle = _fused_verify_tail(
        ssh_target="u@h",
        remote_path="/x",
        job_name="p_canary",
        job_ids=["42"],
        scheduler="sge",
        result_dir="results/r1-canary/task_0",
        expect_output="results/r1-canary/task_0/metrics.json",
        fingerprint="results/r1-canary/task_0/metrics.json",
    )
    # ONE round trip for the whole tail.
    assert len(calls) == 1
    # And ONE script carrying all four sections + the candidate log path.
    script = calls[0]
    for name in ("TAIL", "RUNTIME", "OUTPUT", "FPRINT"):
        assert f"<<<HPC_VTAIL:{name}>>>" in script
    assert "_runtime.json" in script
    assert "sha256sum" in script
    # Every leg parsed from the one read.
    assert bundle["stderr_tail"] == "line-one\nline-two"
    assert bundle["log_path"] == "/x/logs/p_canary.o42.1"
    assert bundle["runtime"] == {
        "status": "present",
        "exit_code": 0,
        "elapsed_sec": None,
        "peak_rss_mb": None,
        "gpu_type": None,
    }
    assert bundle["output"] == (True, "ok")
    assert bundle["fingerprint_sha"] == "deadbeefcafe"


def test_fused_verify_tail_torn_read_falls_through_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    """A torn/failed fused read never mints a false verdict: an ssh error yields an
    empty tail + runtime 'absent' (result_dir was requested) and unknown output/
    fingerprint — exactly the per-call read-miss fall-through it replaced."""
    from hpc_agent.ops.verify_canary import _fused_verify_tail

    def _boom(cmd, *, ssh_target):  # noqa: ANN001
        raise errors.RemoteCommandFailed("ssh died", returncode=255)

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _boom)
    bundle = _fused_verify_tail(
        ssh_target="u@h",
        remote_path="/x",
        job_name="p_canary",
        job_ids=["42"],
        scheduler="sge",
        result_dir="results/r1-canary/task_0",
        expect_output=None,
        fingerprint=None,
    )
    assert bundle["stderr_tail"] == ""
    assert bundle["log_path"] is None
    assert bundle["runtime"] == {"status": "absent"}
    assert bundle["output"] is None
    assert bundle["fingerprint_sha"] is None


def _rc2_sidecar_not_found() -> errors.RemoteCommandFailed:
    """The finding-7 poll failure: reporter rc 2 with a DETERMINISTIC structured
    ``sidecar_not_found`` code (a ``-canary2`` sidecar the deploy never shipped)."""
    return errors.RemoteCommandFailed(
        "status reporter failed (rc=2): sidecar_not_found: .hpc/runs/r1-canary.json",
        returncode=2,
        reporter_error_code="sidecar_not_found",
    )


def test_deterministic_reporter_sidecar_not_found_escalates_early(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 7 (2.2): the ONE terminal reporter returns a ``sidecar_not_found``
    fault (rc 2, a file that will NEVER appear by waiting) → escalate immediately
    — NOT the old 1800s spin — to a ``reporter_unreachable`` verdict that names the
    reporter code, the sidecar path polled (derived from the recorded run id), and
    the sibling sidecars that DID ship. The sibling-ls runs exactly once."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    _clk = itertools.count(0.0, 1.0)  # far under the 1800s deadline → escalation
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))

    ls_calls: list[dict] = []

    def _fake_ls(**kw):
        ls_calls.append(kw)
        return ["r1-canary.json"]  # the FIRST canary shipped; -canary2 did not

    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=_rc2_sidecar_not_found(),
        ),
        mock.patch("hpc_agent.infra.cluster_status.ssh_list_run_sidecars", side_effect=_fake_ls),
        # The env-class marker scan must NOT run for a reporter-class escalation.
        mock.patch("hpc_agent.infra.cluster_status.ssh_marker_scan") as m_scan,
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)

    assert out["ok"] is False
    assert out["failure_kind"] == "reporter_unreachable"
    assert "sidecar_not_found" in out["details"]
    assert ".hpc/runs/r1-canary.json" in out["details"]  # the polled path, disclosed
    assert "r1-canary.json" in out["details"]  # the sibling that shipped
    assert "at the terminal poll" in out["details"]  # 2.2: single terminal reporter
    assert len(ls_calls) == 1  # escalated at the terminal reporter, ls ran once
    m_scan.assert_not_called()


def test_transient_polls_ride_budget_not_early_failed(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TRANSIENT poll failure (SSH timeout — OSError) must NOT early-fail: it
    resets the deterministic counter and rides the wait budget (that class
    belongs to the connection breaker). Proven by the marker scan never being
    invoked and the verdict carrying no 'Escalated after' annotation."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    _clk = itertools.count(0.0, 1.0)  # budget=4 → several transient polls then timeout
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))

    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=TimeoutError("ssh to host timed out"),
        ),
        mock.patch("hpc_agent.infra.cluster_status.ssh_marker_scan") as m_scan,
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=4)

    assert out["ok"] is False
    # Rode to the budget with every poll failing → reporter_unreachable, but NOT
    # the deterministic early-fail arm.
    assert out["failure_kind"] == "reporter_unreachable"
    assert "Escalated after" not in out["details"]
    m_scan.assert_not_called()


def test_poll_health_evidence_stamped_under_distinct_key(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a failed LIGHT poll the loop stamps poll-failure evidence (error class +
    rc) under ``last_status.poll_health`` so status-snapshot renders a
    live-but-struggling poller instead of a frozen timestamp. 2.2: the light poll
    (an ``ls`` census / one ``qstat``) can only fail TRANSIENTLY — a broken-env rc
    127 surfaces only at the ONE terminal reporter — so the stamped class is
    ``transient``. The evidence lives under a DISTINCT key — it MUST NOT inject any
    of the complete/running/pending/failed/unknown counts ``classify.settle``
    reads."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary
    from hpc_agent.state.journal import load_run

    _seed_canary(tmp_path)
    _clk = itertools.count(0.0, 1.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))

    # The LIGHT census read fails transiently (an ssh transport blip, rc 255) on
    # every poll → the loop stamps poll_health and rides the budget.
    with mock.patch(
        "hpc_agent.ops.monitor.announce.read_announcements",
        side_effect=errors.RemoteCommandFailed("announce read failed (rc=255)", returncode=255),
    ):
        verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=3)

    rec = load_run(tmp_path, "r1-canary")
    assert rec is not None
    ph = rec.last_status.get("poll_health")
    assert ph is not None
    assert ph["error_class"] == "transient"
    assert ph["consecutive"] == 1
    assert ph["returncode"] == 255
    # Settle-safe: the evidence never wrote a count key settle()/classify_polling read.
    for count_key in ("complete", "running", "pending", "failed", "unknown"):
        assert count_key not in rec.last_status


def test_canary_loop_stamps_watchdog_liveness_each_poll(tmp_path: Path, journal_home: Path) -> None:
    """Finding 12: the canary poll loop stamps §5 watchdog liveness (through the
    ONE shared ``stamp_watchdog_tick`` definition) so the sidecar isn't frozen at
    its submit stamp while polling — status-snapshot sees a live poller, not a
    stall the doctor false-flags."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    stamps: list[tuple] = []

    def _spy(*a, **k):
        stamps.append((a, k))

    with (
        mock.patch("hpc_agent.state.journal.stamp_watchdog_tick", side_effect=_spy),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}},
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from([{"task_id": 0, "content": "[dispatch] ok\n"}]),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)

    assert out["ok"] is True
    assert stamps, "canary loop must stamp watchdog liveness while polling"
    # Stamped for the canary run, carrying the next-poll cadence.
    assert stamps[0][0][0] == "r1-canary"
    assert "next_tick_seconds" in stamps[0][1]


def test_fused_verify_tail_missing_ack_reads_as_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinel-ack (finding-24 class): stdout WITHOUT the trailing ack token —
    a severed/truncated channel — must parse as the miss shape, never as a
    shorter-but-valid section set (a torn RUNTIME could mask a nonzero exit)."""
    from hpc_agent.ops.verify_canary import _fused_verify_tail

    torn = "<<<HPC_VTAIL:TAIL>>>\nFOUND\t/x/logs/p.o1.1\npartial\n"

    def _fake_ssh_run(cmd, *, ssh_target):  # noqa: ANN001
        return _fake_ssh_completed(stdout=torn)

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _fake_ssh_run)
    bundle = _fused_verify_tail(
        ssh_target="u@h",
        remote_path="/x",
        job_name="p",
        job_ids=["1"],
        scheduler="sge",
        result_dir="results/r-canary/task_0",
        expect_output=None,
        fingerprint=None,
    )
    assert bundle["stderr_tail"] == ""
    assert bundle["log_path"] is None
    assert bundle["runtime"] == {"status": "absent"}


# ── run-14: SGE silent vmem-kill hint + canary runtime/memory prior mint ─────


def test_completed_unknown_on_sge_attaches_vmem_kill_hypothesis(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canary that vanished SILENTLY (empty stderr) on an SGE cluster carries
    the h_data vmem-kill hypothesis: the details name the likely cause + the
    qacct confirmation, and failure_features.classified_error is the seam
    classifier's triple — instead of the bland 'inspect the job log'."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)  # cluster=hoffman2 → scheduler family sge
    import itertools

    _clk = itertools.count(0.0, 100.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
    with (
        mock.patch(
            "hpc_agent.ops.monitor.announce.read_announcements",
            return_value=_census(present=0),  # 2.2: light poll → vanished-grace arm
        ),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={
                "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
            },
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            # SILENT death: empty stderr — the observed live shape.
            return_value=_tail_from([{"task_id": 0, "content": ""}]),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["failure_kind"] == "completed_unknown"
    assert "LIKELY CAUSE (hypothesis)" in out["details"]
    assert "h_data" in out["details"]
    assert out["failure_features"]["classified_error"]["error_class"] == "sge_vmem_kill_suspected"


def test_completed_unknown_with_traceback_gets_no_vmem_hint(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stderr naming a DIFFERENT cause disqualifies the hypothesis — the death
    was not silent, so the envelope stays exactly as before run-14."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    import itertools

    _clk = itertools.count(0.0, 100.0)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
    with (
        mock.patch(
            "hpc_agent.ops.monitor.announce.read_announcements",
            return_value=_census(present=0),  # 2.2: light poll → vanished-grace arm
        ),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={
                "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
            },
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            # Not a recognized _FAILURE_MARKERS token, but a vmem disqualifier:
            # a logged retry attempt proves a process-level error, not SIGKILL.
            return_value=_tail_from(
                [{"task_id": 0, "content": "[hpc-agent] attempt 2/3: python -m train\n"}]
            ),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)
    assert out["failure_kind"] == "completed_unknown"
    assert "LIKELY CAUSE (hypothesis)" not in out["details"]


def test_verified_canary_mints_runtime_and_memory_prior(tmp_path: Path, journal_home: Path) -> None:
    """A verified canary whose _runtime.json carried elapsed + peak RSS mints a
    runtime-prior sample under runtimes/<profile>.<cluster>.json (run-14: the
    canary seeds BOTH the runtime and the memory prior before the array runs),
    and stamps both measurements onto its own sidecar."""
    from hpc_agent.ops.verify_canary import verify_canary
    from hpc_agent.state.runs import read_canary_elapsed_sec, write_run_sidecar
    from hpc_agent.state.runtime_prior import read_samples

    _seed_canary(tmp_path)
    write_run_sidecar(
        tmp_path,
        run_id="r1-canary",
        cmd_sha="feedface",
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=1,
        tasks_py_sha="",
    )
    with (
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            return_value={"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}},
        ),
        mock.patch(
            "hpc_agent.ops.verify_canary._fused_verify_tail",
            return_value=_tail_from(
                [{"task_id": 0, "content": "[dispatch] task_id=0 run_id=r1\n"}],
                runtime={
                    "status": "present",
                    "exit_code": 0,
                    "elapsed_sec": 573,
                    "peak_rss_mb": 2048,
                    "gpu_type": None,
                },
            ),
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=10)
    assert out["ok"] is True
    # Sidecar stamp: both measurements.
    assert read_canary_elapsed_sec(tmp_path, "r1-canary") == 573
    # Prior sample: keyed (profile=p, cluster=hoffman2), CPU bucket, mem leg set.
    samples = read_samples(tmp_path, profile="p", cluster="hoffman2")
    assert len(samples) == 1
    s = samples[0]
    assert s["run_id"] == "r1-canary" and s["task_id"] == 0
    assert s["elapsed_sec"] == 573
    assert s["peak_host_mem_mb"] == 2048
    assert s["gpu_type"] == "cpu"
    assert s["cmd_sha"] == "feedface"


# ── 2.2 WS-CANARY: liveness split — heavy reporter EXACTLY once, at terminal ──
#
# The canary poll loop used to run the FULL cluster-side status reporter
# (ssh_status_report → python -m …reduce.status) on EVERY tick. The liveness
# split makes the loop issue only LIGHT reads per tick (read_announcements /
# ssh_batch_scheduler_states — no cluster-side python) and run the heavy reporter
# EXACTLY once, at terminal. rc 126/127 broken-env / torn-sidecar escalation
# moves to that single terminal reporter (never a full-budget spin); a transient
# breaker trip at it is waited out + retried once (G1) so a finished job's verdict
# is never lost.


def test_heavy_reporter_invoked_exactly_once_at_terminal(
    tmp_path: Path, journal_home: Path
) -> None:
    """The FULL status reporter runs EXACTLY once — at terminal — while the poll
    loop issues only LIGHT liveness reads. Drive several in-flight census polls,
    then a complete census; count the heavy reporter calls (must be 1)."""
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    reporter_calls: list[dict] = []

    def _count_report(**kw):
        reporter_calls.append(kw)
        return {"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}}

    in_flight = _census(present=1, complete=0, missing=1)  # → pending=1, keep polling
    done_census = _census(present=1, complete=1, missing=0)  # → COMPLETE, break
    ann_calls: list[dict] = []

    def _census_seq(**kw):
        ann_calls.append(kw)
        return [in_flight, in_flight, done_census][min(len(ann_calls) - 1, 2)]

    sched_calls: list[dict] = []

    def _sched(**kw):
        sched_calls.append(kw)
        return {}

    with (
        mock.patch("hpc_agent.ops.monitor.announce.read_announcements", side_effect=_census_seq),
        mock.patch("hpc_agent.infra.cluster_status.ssh_batch_scheduler_states", side_effect=_sched),
        mock.patch("hpc_agent.infra.cluster_status.ssh_status_report", side_effect=_count_report),
        mock.patch(_VTAIL, return_value=_tail_from([{"task_id": 0, "content": "[dispatch] ok\n"}])),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)

    assert out["ok"] is True
    # The HEAVY reporter ran exactly once, at terminal — not per tick.
    assert len(reporter_calls) == 1
    # The LIGHT census was the per-tick driver (3 polls: 2 in-flight + 1 complete).
    assert len(ann_calls) == 3
    # The scheduler-state fallback never fired (the census was present every tick).
    assert sched_calls == []


def test_breaker_open_at_terminal_recovers_after_deadline_wait_and_retry(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1: a transient ``SshCircuitOpen`` at the ONE terminal reporter must NOT
    lose a finished job's verdict — ``_terminal_status_report`` waits out the
    breaker deadline (``exc.deadline``) and retries ONCE (the sanctioned half-open
    probe), after which the verdict is recovered."""
    from hpc_agent.errors import SshCircuitOpen
    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: 1000.0)
    slept: list[float] = []
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.sleep", slept.append)

    breaker = SshCircuitOpen("breaker open for user@h until deadline")
    breaker.host = "user@h"
    breaker.deadline = 1060.0  # 60s past now(1000) → within the breaker wait cap
    done = {"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0}}
    with (
        mock.patch("hpc_agent.infra.cluster_status.ssh_status_report", side_effect=[breaker, done]),
        mock.patch(_VTAIL, return_value=_tail_from([{"task_id": 0, "content": "[dispatch] ok\n"}])),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)

    assert out["ok"] is True  # verdict recovered after the ONE retry
    assert out["failure_kind"] is None
    # It waited out the breaker deadline (≈60s + slack) before the single retry.
    assert slept and slept[-1] >= 60.0


def test_broken_env_leaves_queue_escalates_at_terminal_reporter_not_budget(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken-env canary fails in its preamble and leaves the scheduler queue
    with no announce dir → the LIGHT poll goes all-zero (vanished grace) → the ONE
    terminal reporter hits rc 127 → escalate loud+fast (the ``.hpc_failed`` marker
    scan proves failure → ``canary_failed``), NEVER the full wait budget."""
    import itertools

    from hpc_agent.ops.verify_canary import verify_canary

    _seed_canary(tmp_path)
    _clk = itertools.count(0.0, 100.0)  # spans the 30s grace by the 2nd poll
    monkeypatch.setattr("hpc_agent.ops.verify_canary.time.monotonic", lambda: next(_clk))
    with (
        mock.patch(
            "hpc_agent.ops.monitor.announce.read_announcements",
            return_value=_census(present=0),
        ),
        mock.patch("hpc_agent.infra.cluster_status.ssh_status_report", side_effect=_rc127()),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_marker_scan",
            return_value={"failed_markers": ["r1-canary.0.failed"], "count": 1},
        ),
    ):
        out = verify_canary(tmp_path, canary_run_id="r1-canary", wait_budget_sec=1800)

    assert out["ok"] is False
    assert out["failure_kind"] == "canary_failed"
    assert "rc=127" in out["details"]
    assert "r1-canary.0.failed" in out["details"]
