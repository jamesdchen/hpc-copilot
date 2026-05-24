"""Adaptive poll backoff: an idle run must back off its sleep duration.

Hot-path perf fix. A 4h job at the default 60s poll fires ~480 SSH +
remote-status round-trips even when nothing is changing tick-to-tick.
``monitor_flow`` now tracks a status fingerprint and, after K
consecutive identical polls, doubles its effective sleep up to a
5-minute cap. Any state change snaps the interval back to the user-set
floor.

The test injects ``_sleep`` and ``_now`` (the same seams the existing
flow tests use) and stubs ``runner.record_status`` to return a fixed
status snapshot, then asserts the sleeps grow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import (
    _MAX_ADAPTIVE_POLL_SECONDS,
    _UNCHANGED_POLLS_BEFORE_BACKOFF,
    monitor_flow,
)
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260521-120000-bbb"


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


def _seed_record(experiment_dir: Path, **overrides: Any) -> RunRecord:
    base = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "p",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-05-21T12:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        # in_flight status: 2 of 4 complete, none failed, nothing pending.
        # Stays constant across polls so the fingerprint repeats.
        "last_status": {
            "complete": 2,
            "running": 2,
            "pending": 0,
            "failed": 0,
            "checked_at": "2026-05-21T12:00:00+00:00",
        },
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def test_unchanged_status_backs_off_then_caps(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 polls with identical status: sleep[4] > sleep[0]; all <= cap."""
    seed = _seed_record(experiment)

    # Stub record_status: always return the seed record (status unchanged).
    def _fake_record_status(
        experiment_dir: Path,
        run_id: str,
        **kwargs: Any,
    ) -> RunRecord:
        return seed

    # Patch the symbols the flow module bound at import time. After the
    # PR 3.1 reorg, monitor_flow imports record_status and mark_terminal
    # directly from hpc_agent.ops.monitor.{status,reconcile}.
    monkeypatch.setattr(monitor_flow_module, "record_status", _fake_record_status)

    # Stub mark_terminal so a stray COMPLETE path can't corrupt state — but
    # with `complete=2 < total_tasks=4` and `running=2`, we should never
    # hit terminal.
    monkeypatch.setattr(
        monitor_flow_module,
        "mark_terminal",
        lambda *a, **k: seed,
    )

    sleeps: list[float] = []
    fake_clock = {"t": 0.0}
    POLL_FLOOR = 60.0
    TARGET_POLLS = 5

    class _Stop(Exception):
        """Sentinel to exit the loop after exactly TARGET_POLLS sleeps."""

    def _sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        # Advance the fake clock so wall-clock budget accounting reflects
        # the (adaptive) sleep — keeps the budget check honest.
        fake_clock["t"] += float(seconds)
        if len(sleeps) >= TARGET_POLLS:
            raise _Stop

    def _now() -> float:
        return fake_clock["t"]

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=POLL_FLOOR,
        # Budget large enough that we exit via _Stop, not via timeout.
        wall_clock_budget_seconds=10_000_000,
        auto_combine_waves=False,
    )

    with pytest.raises(_Stop):
        monitor_flow(experiment, spec=spec, _sleep=_sleep, _now=_now)

    # We must have captured exactly TARGET_POLLS sleeps.
    assert len(sleeps) == TARGET_POLLS, sleeps

    # Floor never violated.
    assert all(s >= POLL_FLOOR for s in sleeps), sleeps
    # Cap never violated.
    assert all(s <= _MAX_ADAPTIVE_POLL_SECONDS for s in sleeps), sleeps
    # First sleep is exactly the floor (no prior fingerprint → no backoff).
    assert sleeps[0] == POLL_FLOOR

    # Backoff actually kicked in: the 5th poll's sleep > 1st poll's sleep.
    assert sleeps[-1] > sleeps[0], (
        f"adaptive backoff didn't grow: first={sleeps[0]} last={sleeps[-1]} sleeps={sleeps}"
    )

    # Specifically, with K=2 and POLL_FLOOR=60, the expected trace is
    #   60, 60, 120, 240, 300
    # - poll 1: no prior fingerprint -> unchanged_count=0, sleep=60
    # - poll 2: matches -> unchanged_count=1 (< K), sleep=60
    # - poll 3: matches -> unchanged_count=2 (>= K), double to 120
    # - poll 4: matches -> double to 240
    # - poll 5: matches -> would double to 480, capped at 300
    assert sleeps == [60.0, 60.0, 120.0, 240.0, 300.0], sleeps


def test_state_change_resets_backoff(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status change snaps the effective interval back to the floor."""
    seed = _seed_record(experiment)

    POLL_FLOOR = 60.0

    # Drive a sequence of status snapshots (K=2):
    #   poll 1: A   (no prior fp, unchanged_count=0, sleep -> 60)
    #   poll 2: A   (matches, unchanged_count=1, < K, sleep -> 60)
    #   poll 3: A   (matches, unchanged_count=2, >= K, sleep -> 120)
    #   poll 4: A   (matches, unchanged_count=3, sleep -> 240)
    #   poll 5: B   (CHANGED -> reset to floor 60)
    snapshots = [
        {"complete": 2, "running": 2, "pending": 0, "failed": 0},
        {"complete": 2, "running": 2, "pending": 0, "failed": 0},
        {"complete": 2, "running": 2, "pending": 0, "failed": 0},
        {"complete": 2, "running": 2, "pending": 0, "failed": 0},
        {"complete": 3, "running": 1, "pending": 0, "failed": 0},  # change!
    ]
    call_idx = {"n": 0}

    def _fake_record_status(
        experiment_dir: Path,
        run_id: str,
        **kwargs: Any,
    ) -> RunRecord:
        snap = snapshots[min(call_idx["n"], len(snapshots) - 1)]
        call_idx["n"] += 1
        # Return a fresh record with the new status.
        new = RunRecord(
            run_id=seed.run_id,
            profile=seed.profile,
            cluster=seed.cluster,
            ssh_target=seed.ssh_target,
            remote_path=seed.remote_path,
            job_name=seed.job_name,
            job_ids=list(seed.job_ids),
            total_tasks=seed.total_tasks,
            submitted_at=seed.submitted_at,
            experiment_dir=seed.experiment_dir,
            last_status=dict(snap),
        )
        return new

    monkeypatch.setattr(monitor_flow_module, "record_status", _fake_record_status)
    monkeypatch.setattr(monitor_flow_module, "mark_terminal", lambda *a, **k: seed)

    sleeps: list[float] = []
    fake_clock = {"t": 0.0}

    class _Stop(Exception):
        pass

    def _sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        fake_clock["t"] += float(seconds)
        if len(sleeps) >= 5:
            raise _Stop

    def _now() -> float:
        return fake_clock["t"]

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=POLL_FLOOR,
        wall_clock_budget_seconds=10_000_000,
        auto_combine_waves=False,
    )

    with pytest.raises(_Stop):
        monitor_flow(experiment, spec=spec, _sleep=_sleep, _now=_now)

    # The 5th poll observed a change -> back to floor.
    assert sleeps == [60.0, 60.0, 120.0, 240.0, 60.0], sleeps


def test_backoff_constants_are_sane() -> None:
    """Cap is 300s (5 min); K is small (<=3)."""
    assert _MAX_ADAPTIVE_POLL_SECONDS == 300.0
    assert 1 <= _UNCHANGED_POLLS_BEFORE_BACKOFF <= 3
