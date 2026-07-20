"""Reconcile's crash-only Phase-1 announce fast path.

The dispatcher announces each task's terminal state as a filename-encoded
marker; ``reconcile`` reads those FIRST and, on a FULL announcement, settles the
lifecycle exactly as the reporter-backed settle arm would for the same counts —
WITHOUT paying the status-reporter walk (run-12 findings 20/24). A PARTIAL
announcement is progress evidence only and never settles; zero markers fall
through to the legacy probe path byte-identically (the package-wide
``_no_announcements`` autouse default in conftest).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.monitor import reconcile as recon
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, total_tasks: int = 4, job_ids=("100", "200")) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=total_tasks,
        submitted_at="2026-07-11T00:00:00Z",
        experiment_dir="/exp",
        status="in_flight",
    )


def _reporter_tripwire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any status-reporter invocation so a test can assert it never ran."""
    calls: list[str] = []

    def _status(**_kw):
        calls.append("status")
        return {"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}}

    monkeypatch.setattr(recon, "_ssh_status_report", _status)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: set())
    return calls


def _count_harvests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _fake(experiment_dir, run_id, *, terminal_cause, record=None, **_kw):
        calls.append(terminal_cause)
        # Mirror the real guard's LAST step: append a durable receipt to the
        # ledger, so the reconcile backstop's receipt-derived idempotency holds
        # (a real harvest ALWAYS writes a marker — a receipt-less fake would let
        # the terminal-with-no-receipt backstop re-fire on every re-reconcile).
        append_jsonl_line(
            harvest_marker_path(experiment_dir, run_id),
            {"run_id": run_id, "terminal_cause": terminal_cause, "harvest_ok": True},
        )
        return {}

    monkeypatch.setattr(recon, "harvest_on_terminal", _fake)
    return calls


def _stub_announcements(monkeypatch: pytest.MonkeyPatch, payload: dict[str, int]) -> None:
    monkeypatch.setattr(
        recon, "read_announcements", lambda *, ssh_target, remote_path, run_id, task_count: payload
    )


def test_full_complete_settles_without_reporter(tmp_path, monkeypatch):
    reporter = _reporter_tripwire(monkeypatch)
    harvests = _count_harvests(monkeypatch)
    upsert_run(tmp_path, _record("done_r1", total_tasks=4))
    _stub_announcements(monkeypatch, {"announced": 4, "complete": 4, "failed": 0, "missing": 0})

    result = recon.reconcile(tmp_path, "done_r1", scheduler="sge")

    assert result.status == "complete"
    last = result.last_status or {}
    assert last["verdict_reason"] == "all_tasks_complete"
    assert last["verdict_source"] == "task_announcements"
    # The whole point: the 20-25 min reporter walk was NOT paid.
    assert reporter == []
    # Guaranteed harvest fired once on the in_flight→complete transition.
    assert harvests == ["complete"]


def test_announce_settle_discloses_source_not_liveness_override(tmp_path, monkeypatch):
    # #19 asymmetry pin (verifier-E nit): the two liveness-independent settle
    # arms disclose through DIFFERENT channels because their liveness
    # relationship differs in KIND. Entry B (reconcile's strict-all-complete
    # reporter arm) QUERIES the scheduler and OVERRIDES a still-alive record —
    # it stamps ``scheduler_alive_at_settle: True`` (pinned in
    # test_reconcile_settle_liveness.py). The announce fast path NEVER queries
    # liveness (it runs before the probes — that is the point), so it must NOT
    # carry that key: a ``True`` there would claim a reading this arm never
    # took. Its disclosure is ``verdict_source: task_announcements`` instead.
    # A naive "consistency" fix stamping the announce arm True goes RED here.
    reporter = _reporter_tripwire(monkeypatch)
    _count_harvests(monkeypatch)
    upsert_run(tmp_path, _record("asym_r1", total_tasks=4))
    _stub_announcements(monkeypatch, {"announced": 4, "complete": 4, "failed": 0, "missing": 0})

    result = recon.reconcile(tmp_path, "asym_r1", scheduler="sge")

    assert result.status == "complete"
    last = result.last_status or {}
    assert last["verdict_source"] == "task_announcements"
    assert "scheduler_alive_at_settle" not in last
    assert reporter == []


def test_full_failed_routes_to_failure_settle(tmp_path, monkeypatch):
    reporter = _reporter_tripwire(monkeypatch)
    harvests = _count_harvests(monkeypatch)
    # _gather_failure_features tails a log; stub the fetch (best-effort seam).
    _log = {"content": "Traceback: boom", "path": "/log", "task_id": 0, "job_id": "100"}
    monkeypatch.setattr(
        "hpc_agent.infra.cluster_logs.fetch_task_logs",
        lambda **_kw: [_log],
    )
    upsert_run(tmp_path, _record("bad_r2", total_tasks=4))
    _stub_announcements(monkeypatch, {"announced": 4, "complete": 1, "failed": 3, "missing": 0})

    result = recon.reconcile(tmp_path, "bad_r2", scheduler="sge")

    assert result.status == "failed"
    last = result.last_status or {}
    assert last["verdict_reason"] == "positive_failure_evidence"
    assert "failure_features" in last
    assert reporter == []
    assert harvests == ["failed"]


def test_partial_mid_flight_skips_reporter_walk(tmp_path, monkeypatch):
    # Rank 19: a PARTIAL announcement on a still-alive run answers the mid-flight
    # lifecycle question from the census + alive probe alone — the 20-25 min
    # status-reporter WALK is NOT paid.
    reporter = _reporter_tripwire(monkeypatch)
    harvests = _count_harvests(monkeypatch)
    # Jobs still alive on the scheduler → mid-flight, census stands in for the walk.
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: {"100", "200"})
    upsert_run(tmp_path, _record("mid_r3", total_tasks=4))
    _stub_announcements(monkeypatch, {"announced": 2, "complete": 2, "failed": 0, "missing": 2})

    result = recon.reconcile(tmp_path, "mid_r3", scheduler="sge")

    # NEVER settle terminal from a partial announcement.
    assert result.status == "in_flight"
    # The reporter walk was SKIPPED — the pinned rank-19 behavior.
    assert reporter == []
    last = result.last_status or {}
    # last_status is census-derived, and discloses the census as the stand-in.
    assert last["status_source"] == "task_announcements"
    assert last["verdict_source"] == "task_announcements"
    assert last["complete"] == 2
    assert last["pending"] == 2  # the two not-yet-terminal tasks (missing)
    # Raw progress evidence still rides along under task_announcements.
    assert last.get("task_announcements") == {
        "announced": 2,
        "complete": 2,
        "failed": 0,
        "missing": 2,
    }
    assert harvests == []  # no terminal transition


def test_partial_but_nothing_alive_walks_to_settle(tmp_path, monkeypatch):
    # Rank 19 settle path: a PARTIAL census with NO live jobs is a terminal
    # situation whose verdict needs per-task detail — so the reporter walk DOES
    # run (the one announce-present tick that still walks), then settles.
    reporter = _reporter_tripwire(monkeypatch)
    harvests = _count_harvests(monkeypatch)
    # Nothing alive on the scheduler.
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: set())
    upsert_run(tmp_path, _record("gone_r5", total_tasks=4))
    _stub_announcements(monkeypatch, {"announced": 2, "complete": 2, "failed": 0, "missing": 2})

    result = recon.reconcile(tmp_path, "gone_r5", scheduler="sge")

    # The walk ran because the settle verdict needs per-task evidence.
    assert reporter == ["status"]
    # The tripwire reporter reports zero complete → incomplete + nothing alive +
    # no positive failure evidence → abandoned (the shared settle classifier).
    assert result.status == "abandoned"
    assert harvests == ["abandoned"]


def test_zero_markers_is_old_path(tmp_path, monkeypatch):
    # The conftest default already returns zero announcements; assert the legacy
    # probe path runs and the fast path stays inert (byte-identical for old runs).
    reporter = _reporter_tripwire(monkeypatch)
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: {"100", "200"})
    upsert_run(tmp_path, _record("old_r4", total_tasks=4))

    result = recon.reconcile(tmp_path, "old_r4", scheduler="sge")

    assert reporter == ["status"]  # reporter walk ran (old path)
    assert "task_announcements" not in (result.last_status or {})


def test_announce_read_failure_is_stamped_never_silent(tmp_path, monkeypatch):
    """The announce-read swallow (``except Exception: _announce = None``) used
    to skip the alive-independent fast path with NO trace — a transient ssh
    hiccup invisibly removed the only settle path that ignores scheduler
    liveness (the 2h31m stranded-in_flight drill). The failure is now
    disclosed exactly like the alive-check/reporter probe failures in the
    same function: a ``warnings`` entry PLUS the machine-readable
    ``announce_probe: "failed"`` stamp in ``last_status``.
    """
    reporter = _reporter_tripwire(monkeypatch)
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: {"100", "200"})

    def _boom(**_kw):
        raise errors.RemoteCommandFailed("announce read failed (rc=255): connection reset")

    monkeypatch.setattr(recon, "read_announcements", _boom)
    upsert_run(tmp_path, _record("blip_r8", total_tasks=4))

    result = recon.reconcile(tmp_path, "blip_r8", scheduler="sge")

    # The legacy probe path still ran byte-identically (the fast path is
    # best-effort) — still alive → in_flight.
    assert reporter == ["status"]
    assert result.status == "in_flight"
    last = result.last_status or {}
    # … but the skipped fast path is no longer silent.
    assert last["announce_probe"] == "failed"
    warnings = last.get("warnings") or []
    assert any("announce read" in w for w in warnings)
