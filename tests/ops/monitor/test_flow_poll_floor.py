"""Paced polling (#3): the connection-pacing poll-interval floor.

A cluster banned us for opening SSH connections too fast. The fix adds
a minimum poll-interval floor (``HPC_STATUS_POLL_INTERVAL_SEC``, default
10s — AiiDA's ``minimum_job_poll_interval``) applied to the spec's
``poll_interval_seconds`` so no spec / campaign can poll faster than the
floor, plus an env-tunable adaptive-backoff cap
(``HPC_STATUS_POLL_MAX_SEC``, default 300s).

These tests exercise the pure ``_floor_poll_interval`` / ``_env_float``
helpers directly, then drive ``monitor_flow`` end-to-end to prove the
floor lands on the actual sleep durations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260521-130000-ccc"


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
        job_name="p",
        job_ids=["9001"],
        total_tasks=4,
        submitted_at="2026-05-21T13:00:00+00:00",
        experiment_dir=str(experiment_dir),
        last_status={
            "complete": 2,
            "running": 2,
            "pending": 0,
            "failed": 0,
            "checked_at": "2026-05-21T13:00:00+00:00",
        },
    )
    upsert_run(experiment_dir, rec)
    return rec


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_floor_raises_below_floor() -> None:
    """A request below the floor is lifted to the floor (default 10s)."""
    assert monitor_flow_module._MIN_POLL_INTERVAL_SECONDS == 10.0
    # The spec's ge=5 lower bound is below the 10s floor.
    assert monitor_flow_module._floor_poll_interval(5.0) == 10.0


def test_floor_honors_larger_request() -> None:
    """A request above the floor is honored unchanged."""
    assert monitor_flow_module._floor_poll_interval(60.0) == 60.0
    assert monitor_flow_module._floor_poll_interval(300.0) == 300.0


def test_env_float_helper_parses_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_env_float` reads valid floats and falls back on unset/invalid/negative."""
    monkeypatch.delenv("HPC_X_FLOAT", raising=False)
    assert monitor_flow_module._env_float("HPC_X_FLOAT", 7.5) == 7.5
    monkeypatch.setenv("HPC_X_FLOAT", "12.5")
    assert monitor_flow_module._env_float("HPC_X_FLOAT", 7.5) == 12.5
    monkeypatch.setenv("HPC_X_FLOAT", "not-a-number")
    assert monitor_flow_module._env_float("HPC_X_FLOAT", 7.5) == 7.5
    monkeypatch.setenv("HPC_X_FLOAT", "-3")
    assert monitor_flow_module._env_float("HPC_X_FLOAT", 7.5) == 7.5


def test_env_override_raises_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raised floor lifts a small request up to the new floor.

    The constant is read from ``HPC_STATUS_POLL_INTERVAL_SEC`` at import
    time; ``monitor_flow`` is a registered ``@primitive`` (not reloadable
    without a double-registration error), so we exercise the *effect* by
    monkeypatching the module-level constant the helper reads.
    """
    monkeypatch.setattr(monitor_flow_module, "_MIN_POLL_INTERVAL_SECONDS", 45.0)
    assert monitor_flow_module._floor_poll_interval(10.0) == 45.0
    assert monitor_flow_module._floor_poll_interval(60.0) == 60.0


def test_floor_cannot_exceed_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A floor mis-set above the cap is clamped to the cap, not the request."""
    monkeypatch.setattr(monitor_flow_module, "_MIN_POLL_INTERVAL_SECONDS", 9999.0)
    monkeypatch.setattr(monitor_flow_module, "_MAX_ADAPTIVE_POLL_SECONDS", 120.0)
    # floor (9999) > cap (120) -> the effective floor is clamped to 120.
    assert monitor_flow_module._floor_poll_interval(5.0) == 120.0


# ---------------------------------------------------------------------------
# End-to-end: the floor lands on the actual sleeps
# ---------------------------------------------------------------------------


def test_floor_applied_to_loop_sleeps(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec asking for 5s sleeps gets the 10s floor on every sleep."""
    seed = _seed_record(experiment)

    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        lambda experiment_dir, run_id, **kwargs: seed,
    )
    monkeypatch.setattr(monitor_flow_module, "mark_terminal", lambda *a, **k: seed)

    sleeps: list[float] = []
    clock = {"t": 0.0}

    class _Stop(Exception):
        pass

    def _sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        clock["t"] += float(seconds)
        if len(sleeps) >= 2:
            raise _Stop

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5.0,  # spec ge=5, below the 10s floor
        wall_clock_budget_seconds=10_000_000,
        auto_combine_waves=False,
    )

    with pytest.raises(_Stop):
        monitor_flow(experiment, spec=spec, _sleep=_sleep, _now=lambda: clock["t"])

    # The 5s request was floored to 10s; no sleep is below the floor.
    assert sleeps[0] == 10.0, sleeps
    assert all(s >= 10.0 for s in sleeps), sleeps
