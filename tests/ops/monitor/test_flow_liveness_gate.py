"""Rank 4 (preamble-free liveness poll) + rank 21 (transient backoff reset).

Rank 4: while a job is queued/running the monitor pull leg must confirm liveness
with a single preamble-free scheduler-state query (``_census_liveness_probe``,
reusing the already-built ``ssh_batch_scheduler_states`` spine — no python, no
login-shell conda) and MUST NOT run the full conda-activated reporter walk
(``record_status``). The heavyweight walk runs exactly once — the tick the job
leaves the queue (terminal) — plus a bounded heartbeat that keeps the §5
client-alive marker fresh. Scheduler silence (probe returns ``None``) must degrade
to the walk, never settle terminal.

Rank 21: a faulted poll carries no fresh fingerprint, so the adaptive backoff
that grew from PRE-fault quiet no longer applies; a transient blip resets the
effective interval to the floor instead of sleeping a stale backed-off interval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260521-120000-liv"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_record(experiment_dir: Path, **overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "p",
        "job_ids": ["9001"],
        "backend": "slurm",
        "total_tasks": 4,
        "submitted_at": "2026-05-21T12:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "last_status": {
            "complete": 0,
            "running": 0,
            "pending": 4,
            "failed": 0,
            "checked_at": "2026-05-21T12:00:00+00:00",
        },
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _one_shot_stop() -> tuple[list[float], Any, Any, type[Exception]]:
    """A ``(_sleep, _now)`` pair that stops the loop after one sleep."""
    sleeps: list[float] = []
    clock = {"t": 0.0}

    class _Stop(Exception):
        pass

    def _sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        clock["t"] += float(seconds)
        raise _Stop

    def _now() -> float:
        return clock["t"]

    return sleeps, _sleep, _now, _Stop


def test_queued_running_tick_makes_zero_reporter_walks(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE PIN: a live-on-scheduler pull-leg tick runs NO conda-activated walk.

    ``_census_liveness_probe`` reports a live job id; ``record_status`` (the full
    reporter walk) must never be called. The tick is recorded from the synthesized
    scheduler-liveness status (``status_source == "scheduler_liveness"``) and the
    run stays in flight.
    """
    _seed_record(experiment)

    # Job is alive on the scheduler this tick.
    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", lambda record: {"9001"})

    walk_calls: list[int] = []

    def _no_walk(*_a: Any, **_k: Any) -> RunRecord:
        walk_calls.append(1)
        raise AssertionError("full reporter walk ran while the job was queued/running")

    monkeypatch.setattr(monitor_flow_module, "record_status", _no_walk)

    tick_summaries: list[dict[str, Any]] = []
    real_append = monitor_flow_module._append_tick

    def _capture_append(
        experiment_dir: Path, run_id: str, *, summary: dict[str, Any], **kw: Any
    ) -> Any:
        tick_summaries.append(dict(summary))
        return real_append(experiment_dir, run_id, summary=summary, **kw)

    monkeypatch.setattr(monitor_flow_module, "_append_tick", _capture_append)

    sleeps, _sleep, _now, _Stop = _one_shot_stop()
    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=60.0,
        wall_clock_budget_seconds=10_000_000,
        auto_combine_waves=False,
    )

    with pytest.raises(_Stop):
        monitor_flow(experiment, spec=spec, _sleep=_sleep, _now=_now)

    assert walk_calls == [], "the reporter walk must NOT run on a queued/running tick"
    assert sleeps == [60.0], sleeps  # one cheap tick, slept the floor
    assert tick_summaries, "a tick must have been recorded"
    assert tick_summaries[-1].get("status_source") == "scheduler_liveness"
    # The synthetic in-flight status keeps the run from settling.
    assert tick_summaries[-1].get("pending") == 4
    assert tick_summaries[-1].get("complete") == 0


def test_full_reporter_walk_runs_once_when_job_leaves_the_queue(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean scheduler query holding NOTHING alive => the ONE terminal walk runs."""
    seed = _seed_record(experiment)

    # Scheduler ran clean and holds nothing alive -> job left the queue.
    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", lambda record: set())

    walk_calls: list[int] = []

    def _walk_complete(experiment_dir: Path, run_id: str, **_k: Any) -> RunRecord:
        walk_calls.append(1)
        return RunRecord(
            run_id=seed.run_id,
            profile=seed.profile,
            cluster=seed.cluster,
            ssh_target=seed.ssh_target,
            remote_path=seed.remote_path,
            job_name=seed.job_name,
            job_ids=list(seed.job_ids),
            backend=seed.backend,
            total_tasks=seed.total_tasks,
            submitted_at=seed.submitted_at,
            experiment_dir=seed.experiment_dir,
            last_status={"complete": 4, "running": 0, "pending": 0, "failed": 0},
        )

    monkeypatch.setattr(monitor_flow_module, "record_status", _walk_complete)
    monkeypatch.setattr(monitor_flow_module, "mark_terminal", lambda *a, **k: seed)

    result = monitor_flow(
        experiment,
        spec=MonitorFlowSpec(
            run_id=_RUN_ID,
            poll_interval_seconds=60.0,
            wall_clock_budget_seconds=10_000_000,
            auto_combine_waves=False,
        ),
        _sleep=lambda _s: None,
        _now=lambda: 0.0,
    )

    assert walk_calls == [1], "exactly one reporter walk on the terminal (job-left-queue) tick"
    assert result.lifecycle_state == "complete"


def test_probe_none_degrades_to_the_walk(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that could not run (``None``, fail-closed) falls back to the walk.

    Scheduler silence must NEVER settle terminal on its own — it degrades to the
    reporter walk, which owns the settle and its own transient handling.
    """
    seed = _seed_record(experiment)

    # Fail-closed probe: could not determine liveness.
    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", lambda record: None)

    walk_calls: list[int] = []

    def _walk_in_flight(experiment_dir: Path, run_id: str, **_k: Any) -> RunRecord:
        walk_calls.append(1)
        return seed  # still 0/4 complete, 4 pending -> in flight

    monkeypatch.setattr(monitor_flow_module, "record_status", _walk_in_flight)

    sleeps, _sleep, _now, _Stop = _one_shot_stop()
    with pytest.raises(_Stop):
        monitor_flow(
            experiment,
            spec=MonitorFlowSpec(
                run_id=_RUN_ID,
                poll_interval_seconds=60.0,
                wall_clock_budget_seconds=10_000_000,
                auto_combine_waves=False,
            ),
            _sleep=_sleep,
            _now=_now,
        )

    assert walk_calls == [1], "a None probe must degrade to the reporter walk"


def test_heartbeat_forces_a_walk_even_while_alive(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the heartbeat elapsed, the walk runs even though the job is alive.

    Keeps the §5 ``.hpc_last_read`` client-alive marker fresh and gives legacy
    non-announce runs a periodic per-task read.
    """
    seed = _seed_record(experiment)

    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", lambda record: {"9001"})
    # Heartbeat of 0 => a full walk is always "due" (never within the window).
    monkeypatch.setattr(monitor_flow_module, "_REPORTER_HEARTBEAT_SECONDS", 0.0)

    walk_calls: list[int] = []

    def _walk(experiment_dir: Path, run_id: str, **_k: Any) -> RunRecord:
        walk_calls.append(1)
        return seed

    monkeypatch.setattr(monitor_flow_module, "record_status", _walk)

    sleeps, _sleep, _now, _Stop = _one_shot_stop()
    with pytest.raises(_Stop):
        monitor_flow(
            experiment,
            spec=MonitorFlowSpec(
                run_id=_RUN_ID,
                poll_interval_seconds=60.0,
                wall_clock_budget_seconds=10_000_000,
                auto_combine_waves=False,
            ),
            _sleep=_sleep,
            _now=_now,
        )

    assert walk_calls == [1], "the reporter heartbeat must run the walk even while the job is alive"


def test_transient_fault_resets_backoff_to_the_floor(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rank 21: after backing off, a transient poll fault sleeps the FLOOR, not 240s.

    Three unchanged polls ramp the backoff to 120s; a transient
    ``RemoteCommandFailed`` on the 4th poll must reset the effective interval to
    the 60s floor (the backed-off interval rode on a "quiet" inference the faulted
    poll cannot support).
    """
    seed = _seed_record(
        experiment,
        last_status={"complete": 2, "running": 2, "pending": 0, "failed": 0},
    )

    # Force the walk path every tick (probe unavailable -> record_status owns it).
    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", lambda record: None)

    call_idx = {"n": 0}

    def _record_status(experiment_dir: Path, run_id: str, **_k: Any) -> RunRecord:
        n = call_idx["n"]
        call_idx["n"] += 1
        if n >= 3:
            # 4th poll: a transient (non-env, rc != 126/127) reporter fault.
            raise errors.RemoteCommandFailed("transient reporter blip", returncode=2)
        return seed  # unchanged status -> backoff ramps

    monkeypatch.setattr(monitor_flow_module, "record_status", _record_status)
    monkeypatch.setattr(monitor_flow_module, "mark_terminal", lambda *a, **k: seed)

    sleeps: list[float] = []
    clock = {"t": 0.0}

    class _Stop(Exception):
        pass

    def _sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        clock["t"] += float(seconds)
        if len(sleeps) >= 4:
            raise _Stop

    def _now() -> float:
        return clock["t"]

    with pytest.raises(_Stop):
        monitor_flow(
            experiment,
            spec=MonitorFlowSpec(
                run_id=_RUN_ID,
                poll_interval_seconds=60.0,
                wall_clock_budget_seconds=10_000_000,
                auto_combine_waves=False,
            ),
            _sleep=_sleep,
            _now=_now,
        )

    # polls 1-3 succeed (unchanged): 60, 60, 120 (backoff ramps).
    # poll 4 faults transiently: RESET to the floor -> 60 (not the stale 120/240).
    assert sleeps == [60.0, 60.0, 120.0, 60.0], sleeps
