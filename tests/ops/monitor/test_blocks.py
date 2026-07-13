"""Tests for the status human-amplification block verbs — status-snapshot /
status-watch (docs/design/human-amplification-blocks.md §3, §5).

Cluster-free: the composed rings (reconcile-journal / monitor-flow /
decide-monitor-arm) and the journal seams (load_run / find_in_flight_runs /
find_stalled_runs / mark_seen_by_human) are mocked at the ``blocks`` module
boundary, mirroring tests/ops/submit/test_blocks.py. These assert the block
orchestration + brief digestion, never SSH or a scheduler. (The package-level
conftest autouse-stubs the guaranteed-harvest seams, so even a real monitor-flow
would not touch a cluster — but here monitor-flow itself is mocked.)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest import mock

import hpc_agent.ops.status_blocks as blocks
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.status_blocks import (
    StatusSnapshotSpec,
    StatusWatchSpec,
)
from hpc_agent.ops.relay_render import render_relay

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "ml_run_abcd1234"


# ── fixtures ─────────────────────────────────────────────────────────────────


def _record(
    *,
    run_id: str = _RUN_ID,
    status: str = "in_flight",
    last_status: dict[str, Any] | None = None,
    last_tick_at: str | None = None,
    last_seen_by_human_at: str | None = None,
    total_tasks: int = 10,
) -> SimpleNamespace:
    """A duck-typed RunRecord carrying just the fields the digest reads."""
    return SimpleNamespace(
        run_id=run_id,
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        status=status,
        last_status=last_status if last_status is not None else {"running": 4, "pending": 6},
        last_tick_at=last_tick_at,
        last_seen_by_human_at=last_seen_by_human_at,
        total_tasks=total_tasks,
    )


def _journal_of(*recs: SimpleNamespace):
    """A ``load_run(experiment_dir, run_id)`` stand-in keyed by run_id.

    The single-run snapshot path loads the requested run AND its paired
    ``-canary`` / parent sibling (#258), so a bare ``return_value=rec`` mock
    would hand the parent record back for the sibling lookup too. This maps
    each id to its own record and returns None for absent siblings, like the
    real journal.
    """
    table = {r.run_id: r for r in recs}
    return lambda _experiment_dir, run_id: table.get(run_id)


def _monitor_result(*, lifecycle_state: str, last_status: dict[str, Any] | None = None):
    from hpc_agent.ops.monitor_flow import MonitorFlowResult

    return MonitorFlowResult(
        run_id=_RUN_ID,
        lifecycle_state=lifecycle_state,
        last_status=(
            last_status
            if last_status is not None
            else {"complete": 10, "running": 0, "pending": 0, "failed": 0}
        ),
        combined_waves=[0, 1],
        failed_waves=[],
        ticks=3,
        elapsed_seconds=42.0,
        escalation_reason=None,
    )


# ── status-snapshot ──────────────────────────────────────────────────────────


def test_snapshot_digests_running_where_and_stamps_watermark(tmp_path: Path) -> None:
    """The snapshot brief carries running-where + the changed-since-last-seen
    delta, and re-stamps the attention watermark AFTER digesting."""
    # last_tick_at is newer than the prior watermark → changed since seen.
    rec = _record(
        last_status={"running": 4, "pending": 6, "checked_at": "2026-07-03T12:00:00+00:00"},
        last_tick_at="2026-07-03T12:00:00+00:00",
        last_seen_by_human_at="2026-07-03T09:00:00+00:00",
    )
    seen_calls: list[tuple[str, str]] = []

    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(rec)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(
            blocks,
            "mark_seen_by_human",
            side_effect=lambda run_id, *, at, experiment_dir: seen_calls.append((run_id, at)),
        ),
    ):
        result = blocks.status_snapshot(
            tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID, now_iso="2026-07-03T15:00:00+00:00")
        )

    assert result.block == "snapshot"
    assert result.stage_reached == "snapshot_clean"
    assert result.needs_decision is False
    # running-where digest: counts projected, cluster/ssh carried.
    row = result.brief["running_where"][0]
    assert row["run_id"] == _RUN_ID
    assert row["cluster"] == "hoffman2"
    assert row["summary"] == {"running": 4, "pending": 6}
    assert row["changed_since_seen"] is True
    # the changed-since-seen delta surfaces the changed run.
    assert [r["run_id"] for r in result.brief["changed_since_seen"]] == [_RUN_ID]
    # watermark re-stamped with the snapshot's `now`.
    assert seen_calls == [(_RUN_ID, "2026-07-03T15:00:00+00:00")]


def test_snapshot_no_watermark_move_when_mark_seen_false(tmp_path: Path) -> None:
    rec = _record(last_seen_by_human_at="2026-07-03T09:00:00+00:00")
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(rec)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human") as m_seen,
    ):
        blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID, mark_seen=False))
    m_seen.assert_not_called()


def test_snapshot_surfaces_stalled_run_evidence(tmp_path: Path) -> None:
    """find_stalled_runs hits → needs_decision + snapshot_anomaly terminator."""
    rec = _record()
    stalled = [
        {
            "run_id": _RUN_ID,
            "status": "in_flight",
            "last_tick_at": "2026-07-03T04:20:00+00:00",
            "next_tick_due": "2026-07-03T04:25:00+00:00",
            "cluster": "hoffman2",
            "ssh_target": "user@hoffman2.idre.ucla.edu",
        }
    ]
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(rec)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=stalled),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        result = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID))

    assert result.stage_reached == "snapshot_anomaly"
    assert result.needs_decision is True
    assert result.brief["stalled_runs"] == stalled


def test_snapshot_failed_run_is_an_anomaly_with_recommendation(tmp_path: Path) -> None:
    rec = _record(status="failed")
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(rec)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        result = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID))

    assert result.stage_reached == "snapshot_anomaly"
    assert result.needs_decision is True
    anomaly = result.brief["anomalies"][0]
    assert anomaly["status"] == "failed"
    # proposed next-action DATA (not LLM prose).
    assert anomaly["recommendation"] == {
        "action": "classify-failed-tasks",
        "then": "resubmit-failed",
    }


def test_snapshot_reconcile_requires_scheduler(tmp_path: Path) -> None:
    from hpc_agent import errors

    with mock.patch.object(blocks, "reconcile") as m_rec:
        try:
            blocks.status_snapshot(
                tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID, reconcile=True, scheduler=None)
            )
        except errors.SpecInvalid:
            pass
        else:  # pragma: no cover - guard must fire
            raise AssertionError("reconcile=True without a scheduler must raise SpecInvalid")
    m_rec.assert_not_called()


def test_snapshot_reconcile_composes_the_ring(tmp_path: Path) -> None:
    rec = _record()
    with (
        mock.patch.object(blocks, "reconcile") as m_rec,
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(rec)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        blocks.status_snapshot(
            tmp_path,
            spec=StatusSnapshotSpec(run_id=_RUN_ID, reconcile=True, scheduler="slurm"),
        )
    m_rec.assert_called_once()
    assert m_rec.call_args.kwargs["scheduler"] == "slurm"


def test_snapshot_single_run_surfaces_canary_sibling(tmp_path: Path) -> None:
    """Regression (proving run #3, finding c): a single-run snapshot must list
    the paired ``<run_id>-canary`` journal entry (#258), marked as a canary
    child of its parent — an in-flight canary used to be invisible."""
    parent = _record(run_id=_RUN_ID, status="complete")
    canary = _record(run_id=f"{_RUN_ID}-canary", status="in_flight")
    seen_calls: list[str] = []

    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(parent, canary)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(
            blocks,
            "mark_seen_by_human",
            side_effect=lambda run_id, *, at, experiment_dir: seen_calls.append(run_id),
        ),
    ):
        result = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID))

    rows = result.brief["running_where"]
    assert [r["run_id"] for r in rows] == [_RUN_ID, f"{_RUN_ID}-canary"]
    parent_row, canary_row = rows
    assert parent_row["is_canary"] is False
    assert parent_row["parent_run_id"] is None
    assert canary_row["is_canary"] is True
    assert canary_row["parent_run_id"] == _RUN_ID
    # Both digested runs get their attention watermark re-stamped.
    assert seen_calls == [_RUN_ID, f"{_RUN_ID}-canary"]


def test_snapshot_canary_run_id_surfaces_its_parent(tmp_path: Path) -> None:
    """The pairing is symmetric: snapshotting the canary id digests the parent too."""
    parent = _record(run_id=_RUN_ID, status="in_flight")
    canary = _record(run_id=f"{_RUN_ID}-canary", status="complete")
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(parent, canary)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        result = blocks.status_snapshot(
            tmp_path, spec=StatusSnapshotSpec(run_id=f"{_RUN_ID}-canary")
        )

    assert {r["run_id"] for r in result.brief["running_where"]} == {
        _RUN_ID,
        f"{_RUN_ID}-canary",
    }


def test_snapshot_fleet_digest_over_in_flight_runs(tmp_path: Path) -> None:
    recs = [_record(run_id="ml_run_a"), _record(run_id="ml_run_b")]
    with (
        mock.patch.object(blocks, "find_in_flight_runs", return_value=recs),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human") as m_seen,
    ):
        result = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=None))

    assert result.run_id is None
    assert [r["run_id"] for r in result.brief["running_where"]] == ["ml_run_a", "ml_run_b"]
    assert m_seen.call_count == 2


# ── watchdog alert delivery (proving run #3: detection without delivery is
# silence) ────────────────────────────────────────────────────────────────────


_ALERT_TS = "2026-07-04T23:25:05+00:00"
_ALERT_MSG = "hpc-agent doctor: driver stalled since 23:01, run pi-estimation-canary — re-arm?"


def _write_alert_line(experiment_dir: Path, line: str) -> Path:
    """Append a raw line to the repo's doctor.alerts.log (hermetic journal home)."""
    from hpc_agent.state.run_record import journal_dir

    log = journal_dir(experiment_dir) / "doctor.alerts.log"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return log


def _snapshot(tmp_path: Path, **spec_kwargs: Any):
    """Run status-snapshot over one healthy mocked run; alerts read for real."""
    rec = _record()
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(rec)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        return blocks.status_snapshot(
            tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID, **spec_kwargs)
        )


def test_snapshot_surfaces_alert_once_then_acknowledges(tmp_path: Path) -> None:
    """Regression (proving run #3): the watchdog's doctor.alerts.log entry must
    reach the human through the status surface — surfaced by exactly one
    snapshot, then acknowledged via the alert watermark. The log itself is an
    audit trail and survives acknowledgment untouched."""
    log = _write_alert_line(tmp_path, f"{_ALERT_TS} {_ALERT_MSG}")

    first = _snapshot(tmp_path)
    assert first.brief["alerts"] == [{"ts": _ALERT_TS, "message": _ALERT_MSG}]

    second = _snapshot(tmp_path)
    assert second.brief["alerts"] == []

    # Acknowledgment is a watermark, never a truncation.
    assert _ALERT_MSG in log.read_text(encoding="utf-8")


def test_snapshot_newer_alert_after_ack_is_surfaced(tmp_path: Path) -> None:
    _write_alert_line(tmp_path, f"{_ALERT_TS} {_ALERT_MSG}")
    _snapshot(tmp_path)  # surfaces + acknowledges the first alert

    newer_ts = "2026-07-05T01:00:00+00:00"
    _write_alert_line(tmp_path, f"{newer_ts} second stall")
    result = _snapshot(tmp_path)
    assert result.brief["alerts"] == [{"ts": newer_ts, "message": "second stall"}]


def test_snapshot_peek_only_does_not_acknowledge_alerts(tmp_path: Path) -> None:
    """mark_seen=False is a peek: neither the run watermark nor the alert
    watermark moves, so the alert stays new for the next snapshot."""
    _write_alert_line(tmp_path, f"{_ALERT_TS} {_ALERT_MSG}")

    first = _snapshot(tmp_path, mark_seen=False)
    second = _snapshot(tmp_path, mark_seen=False)
    assert first.brief["alerts"] == second.brief["alerts"]
    assert second.brief["alerts"] == [{"ts": _ALERT_TS, "message": _ALERT_MSG}]


def test_snapshot_alerts_fail_open_on_corrupt_log(tmp_path: Path) -> None:
    """A corrupt log (no parseable timestamps) yields no alerts, never an error."""
    _write_alert_line(tmp_path, "not-a-timestamp some garbage")
    _write_alert_line(tmp_path, "\x00\x01\x02binary junk")
    _write_alert_line(tmp_path, "")

    result = _snapshot(tmp_path)
    assert result.brief["alerts"] == []
    assert result.stage_reached == "snapshot_clean"


def test_snapshot_no_alert_log_yields_empty_alerts(tmp_path: Path) -> None:
    result = _snapshot(tmp_path)
    assert result.brief["alerts"] == []


# ── status-watch ─────────────────────────────────────────────────────────────


def _watch_spec(*, invocation_argv: str | None = None) -> StatusWatchSpec:
    # detach=False keeps these synchronous — they assert the monitor-flow poll +
    # brief digestion in-process. The default (detach=True) detach-by-contract
    # path is pinned separately in tests/ops/status/test_block_detach.py.
    return StatusWatchSpec(
        monitor=MonitorFlowSpec(run_id=_RUN_ID),
        invocation_argv=invocation_argv,
        detach=False,
    )


def test_watch_clean_terminal_hands_off_to_harvest(tmp_path: Path) -> None:
    """Clean terminal → needs_decision=False + a harvest hand-off hint."""
    with mock.patch.object(
        blocks, "monitor_flow", return_value=_monitor_result(lifecycle_state="complete")
    ) as m_mon:
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    m_mon.assert_called_once()
    assert result.block == "watch"
    assert result.stage_reached == "watch_terminal"
    assert result.needs_decision is False
    handoff = result.brief["harvest_handoff"]
    assert handoff["guaranteed"] is True
    assert _RUN_ID in handoff["harvest_marker"]
    assert "harvest" in handoff["next_block"]


def test_watch_anomaly_surfaces_evidence_brief(tmp_path: Path) -> None:
    """failed → needs_decision=True + a drafted-evidence brief with a recommendation."""
    mon = _monitor_result(
        lifecycle_state="failed",
        last_status={
            "complete": 7,
            "running": 0,
            "pending": 0,
            "failed": 3,
            "failure_features": {
                "classified_error": {"error_class": "oom"},
                "log_path": "/logs/task0.err",
                "cluster_log_tail": "MemoryError",
            },
        },
    )
    with mock.patch.object(blocks, "monitor_flow", return_value=mon):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    assert result.stage_reached == "watch_anomaly"
    assert result.needs_decision is True
    anomaly = result.brief["anomaly"]
    assert anomaly["summary"] == {"complete": 7, "running": 0, "pending": 0, "failed": 3}
    assert anomaly["error_digest"]["classified_error"] == {"error_class": "oom"}
    assert anomaly["recommendation"] == {
        "action": "classify-failed-tasks",
        "then": "resubmit-failed",
    }


def test_watch_abandoned_recommends_reconcile(tmp_path: Path) -> None:
    mon = _monitor_result(
        lifecycle_state="abandoned",
        last_status={"complete": 0, "running": 0, "pending": 0, "failed": 0},
    )
    with mock.patch.object(blocks, "monitor_flow", return_value=mon):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    assert result.stage_reached == "watch_anomaly"
    assert result.brief["anomaly"]["recommendation"]["action"] == "reconcile-journal"


def test_watch_timeout_arms_next_tick_when_argv_supplied(tmp_path: Path) -> None:
    mon = _monitor_result(
        lifecycle_state="timeout",
        last_status={"complete": 2, "running": 8, "pending": 0, "failed": 0},
    )
    with (
        mock.patch.object(blocks, "monitor_flow", return_value=mon),
        mock.patch.object(blocks, "load_run", return_value=_record()),
        mock.patch.object(
            blocks, "decide_monitor_arm", return_value={"arm": "cron", "cadence_sec": 90}
        ) as m_arm,
    ):
        result = blocks.status_watch(
            tmp_path, spec=_watch_spec(invocation_argv="monitor-hpc --run-id " + _RUN_ID)
        )

    assert result.stage_reached == "watch_timeout"
    assert result.needs_decision is True
    m_arm.assert_called_once()
    arm_spec = m_arm.call_args.kwargs["spec"]
    assert arm_spec.summary == {"complete": 2, "running": 8, "pending": 0, "failed": 0}
    assert arm_spec.total_tasks == 10
    assert result.brief["monitor_arm"] == {"arm": "cron", "cadence_sec": 90}


def test_watch_timeout_no_arm_without_argv(tmp_path: Path) -> None:
    mon = _monitor_result(lifecycle_state="timeout")
    with (
        mock.patch.object(blocks, "monitor_flow", return_value=mon),
        mock.patch.object(blocks, "decide_monitor_arm") as m_arm,
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec(invocation_argv=None))

    assert result.stage_reached == "watch_timeout"
    assert "monitor_arm" not in result.brief
    m_arm.assert_not_called()


# ── relay rendering (finding 15: the snapshot relay is FRESH from the journal) ──


def test_snapshot_relay_matches_current_journal_state(tmp_path: Path) -> None:
    """The snapshot result carries a code-rendered relay reflecting the run's
    CURRENT journal state and its own counts."""
    rec = _record(status="in_flight", last_status={"running": 4, "pending": 6})
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(rec)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        result = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID))

    assert "in_flight" in result.relay
    assert "hoffman2" in result.relay
    assert "4 running" in result.relay


def test_snapshot_relay_renders_new_state_after_transition_not_stale(tmp_path: Path) -> None:
    """The staleness fix (finding 15): a snapshot taken after the journal
    advanced to ``complete`` renders the NEW state — never the prior
    ``in_flight`` the agent may have cached in an earlier brief."""
    before = _record(status="in_flight", last_status={"running": 1})
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(before)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        first = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID))
    assert "in_flight" in first.relay

    after = _record(status="complete", last_status={"complete": 16})
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(after)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        second = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID))

    assert "complete" in second.relay
    assert "16 complete" in second.relay
    assert "in_flight" not in second.relay


def test_snapshot_canary_row_counts_do_not_bleed_parent_total(tmp_path: Path) -> None:
    """Status-side finding-15 guarantee: the canary row renders its OWN 1-task
    count and the parent renders its own, so neither bleeds into the other."""
    parent = _record(run_id=_RUN_ID, status="complete", last_status={"complete": 16})
    canary = _record(run_id=f"{_RUN_ID}-canary", status="complete", last_status={"complete": 1})
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(parent, canary)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        result = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID))

    assert "16 complete" in result.relay  # the parent's own count
    assert "1 complete" in result.relay  # the canary's own 1-task count


def test_snapshot_anomaly_relay_names_state_and_action(tmp_path: Path) -> None:
    rec = _record(status="failed", last_status={"complete": 7, "failed": 3})
    with (
        mock.patch.object(blocks, "load_run", side_effect=_journal_of(rec)),
        mock.patch.object(blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(blocks, "mark_seen_by_human"),
    ):
        result = blocks.status_snapshot(tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID))

    assert result.stage_reached == "snapshot_anomaly"
    assert "failed" in result.relay
    assert "classify-failed-tasks" in result.relay


def test_watch_terminal_result_carries_relay(tmp_path: Path) -> None:
    with mock.patch.object(
        blocks, "monitor_flow", return_value=_monitor_result(lifecycle_state="complete")
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    assert _RUN_ID in result.relay
    assert "complete" in result.relay
    assert "hand off to harvest" in result.relay


def test_render_relay_unknown_block_is_empty() -> None:
    """An unknown (block, stage) surfaces the empty string, never a fabricated line."""
    assert render_relay("nope", "whatever", {}) == ""


# ── registry metadata ────────────────────────────────────────────────────────


def test_status_blocks_are_agent_facing_workflows() -> None:
    from hpc_agent._kernel.registry.primitive import get_meta, register_primitives

    register_primitives()
    for name in ("status-snapshot", "status-watch"):
        meta = get_meta(name)
        assert meta.verb == "workflow"
        assert meta.agent_facing is True
        assert meta.cli is not None
        assert meta.cli.spec_arg is True
