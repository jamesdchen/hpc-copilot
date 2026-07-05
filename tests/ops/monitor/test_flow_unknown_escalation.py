"""Bounded-unknown escalation in the ``monitor_flow`` poll loop (finding f).

Proving run #3, findings ledger item f: when the remote workdir vanishes
mid-run (scratch purge / cleanup), the reporter classifies every task
``unknown`` — no live work, no results, no failure evidence — and the poll
loop had no arm that ever terminated on it: ``classify_polling`` returned
``(None, None)`` forever and the loop spun to the wall-clock budget (which
detached watches set enormous). The loop now counts consecutive
unresolved-unknown ticks and the classifier escalates to a terminal
``abandoned`` anomaly at ``UNKNOWN_TICKS_BEFORE_ESCALATION`` — through the
same mark-terminal + guaranteed-harvest path as complete/failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor.classify import (
    POLLING_REASON_UNKNOWN_EXHAUSTED,
    UNKNOWN_TICKS_BEFORE_ESCALATION,
)
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state import run_record
from hpc_agent.state.journal import load_run, update_run_status, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260704-210000-fff"
_ALL_UNKNOWN = {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 4}
_STILL_RUNNING = {"complete": 0, "running": 2, "pending": 2, "failed": 0, "unknown": 0}


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_record(experiment_dir: Path) -> RunRecord:
    rec = RunRecord(
        run_id=_RUN_ID,
        profile="p",
        cluster="c",
        ssh_target="user@host",
        remote_path="/remote",
        job_name="myjob",
        job_ids=["9001"],
        total_tasks=4,
        submitted_at="2026-07-04T21:00:00+00:00",
        experiment_dir=str(experiment_dir),
    )
    upsert_run(experiment_dir, rec)
    return rec


def _record_status_sequence(seq: list[dict[str, Any]]):
    """A record_status stub that persists each summary in *seq* as the tick's
    ``last_status``; the last item repeats once the sequence is exhausted."""
    idx = {"n": 0}

    def _fake(experiment_dir: Path, run_id: str, **_kwargs: Any) -> RunRecord:
        item = seq[min(idx["n"], len(seq) - 1)]
        idx["n"] += 1
        return update_run_status(experiment_dir, run_id, last_status=dict(item))

    return _fake


def _run_flow(experiment: Path, monkeypatch: pytest.MonkeyPatch, seq: list[dict[str, Any]]):
    _seed_record(experiment)
    monkeypatch.setattr(monitor_flow_module, "record_status", _record_status_sequence(seq))
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    harvested: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: harvested.append(k.get("terminal_cause", "?")),
    )
    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        # Budget deliberately enormous: only the escalation arm can terminate.
        wall_clock_budget_seconds=10**9,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)
    return result, harvested


def test_all_unknown_run_terminates_at_the_bound(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tick unresolved-unknown → the loop terminates ABANDONED after
    exactly UNKNOWN_TICKS_BEFORE_ESCALATION ticks (never the budget), marks
    the journal terminal, and fires the guaranteed harvest."""
    result, harvested = _run_flow(experiment, monkeypatch, [_ALL_UNKNOWN])

    assert result.lifecycle_state == LifecycleState.ABANDONED
    assert result.ticks == UNKNOWN_TICKS_BEFORE_ESCALATION
    assert result.escalation_reason is not None
    assert result.escalation_reason.startswith(POLLING_REASON_UNKNOWN_EXHAUSTED)
    # Journal record carries the terminal verdict; harvest fired once for it.
    rec = load_run(experiment, _RUN_ID)
    assert rec is not None and rec.status == LifecycleState.ABANDONED
    assert harvested == [str(LifecycleState.ABANDONED)]


def test_live_tick_resets_the_unknown_streak(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick with live work mid-streak resets the count — escalation needs
    CONSECUTIVE unresolved-unknown ticks, not a lifetime total."""
    below = UNKNOWN_TICKS_BEFORE_ESCALATION - 1
    seq = [_ALL_UNKNOWN] * below + [_STILL_RUNNING] + [_ALL_UNKNOWN]
    result, _ = _run_flow(experiment, monkeypatch, seq)

    assert result.lifecycle_state == LifecycleState.ABANDONED
    # below ticks + 1 live tick + a fresh full streak of unknown ticks.
    assert result.ticks == below + 1 + UNKNOWN_TICKS_BEFORE_ESCALATION
