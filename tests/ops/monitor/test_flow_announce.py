"""Crash-only Phase-2 announce-first consumption in ``monitor_flow``.

The dispatcher announces each task's terminal state as a filename-encoded marker
(``docs/design/crash-only-monitoring.md``). Phase 1 taught ``reconcile`` to read
those FIRST; Phase 2 teaches the MONITOR poll loop to prefer the ONE-readdir
marker census over the per-task status-reporter WALK for the whole lifecycle —
the pull leg is exactly what a NAT idle-drop / reaper severs at terminal
(run-12 findings 20/24: a 20-25 min reporter walk severed mid-flight left a
finished run unverifiable).

These tests pin the consumption seam:

* an announce-PRESENT run resolves its status from the census with **NO walk**
  (``record_status`` is tripwired and asserted never-called),
* a PARTIAL census stays in-flight and never settles terminal (progress rides
  out under ``task_announcements``),
* a PRE-ANNOUNCE run falls back to the reporter walk, **DISCLOSED** in-band
  (``status_source == "status_reporter_walk"``), and
* a MIXED run walks until the first marker lands, then switches to the census.

The package-wide ``_no_announcements`` autouse fixture (conftest) defaults the
census to NOT-present; each test here overrides ``read_announcements`` in its
own body to drive the announce leg.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.ops.resolve_and_recover_flow import ResolveAndRecoverOutcome
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260712-090000-ann"


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
        "job_name": "myjob",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-07-12T09:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "backend": "sge",
        "auto_resume_on_kill": False,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _walk_tripwire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A ``record_status`` that records any invocation so a test can assert the
    per-task reporter walk NEVER ran on the announce leg."""
    calls: list[str] = []

    def _fake(experiment_dir: Path, run_id: str, **_kw: Any) -> RunRecord:
        calls.append(run_id)
        rec = load_run(experiment_dir, run_id)
        assert rec is not None
        return rec

    monkeypatch.setattr(monitor_flow_module, "record_status", _fake)
    return calls


def _harvest_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: calls.append(k.get("terminal_cause", "?")),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    return calls


def _stub_census(monkeypatch: pytest.MonkeyPatch, *census_seq: dict[str, int]) -> None:
    """Drive ``read_announcements`` through *census_seq* (last item repeats)."""
    idx = {"n": 0}

    def _fake(*, ssh_target: str, remote_path: str, run_id: str, task_count: int) -> dict[str, int]:
        item = census_seq[min(idx["n"], len(census_seq) - 1)]
        idx["n"] += 1
        return dict(item)

    monkeypatch.setattr(monitor_flow_module, "read_announcements", _fake)


def _present(complete: int, failed: int, total: int = 4) -> dict[str, int]:
    announced = complete + failed
    return {
        "present": 1,
        "announced": announced,
        "complete": complete,
        "failed": failed,
        "missing": max(0, total - announced),
    }


_ABSENT = {"present": 0, "announced": 0, "complete": 0, "failed": 0, "missing": 4}


def _spec(**overrides: Any) -> MonitorFlowSpec:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "poll_interval_seconds": 5,
        "wall_clock_budget_seconds": 10_000,
        "auto_combine_waves": False,
    }
    base.update(overrides)
    return MonitorFlowSpec(**base)


def test_full_complete_census_settles_without_walk(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FULL announce census (announced == total, all complete) settles the run
    COMPLETE from ONE readdir — the per-task reporter walk NEVER runs — and the
    guaranteed harvest fires on the transition."""
    _seed_record(experiment)
    walk = _walk_tripwire(monkeypatch)
    harvested = _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(4, 0))

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert walk == []  # the 20-25 min reporter walk was NOT paid
    assert harvested == ["complete"]
    # Provenance marked for every downstream reader.
    assert result.last_status.get("status_source") == "task_announcements"
    assert result.last_status.get("verdict_source") == "task_announcements"


def test_full_failed_census_settles_failed_without_walk(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FULL census with positive failure evidence (all announced, ≥1 failed)
    settles FAILED with no walk; the recover composite is behavior-neutral
    (not opted in) so the run surfaces FAILED."""
    _seed_record(experiment)
    walk = _walk_tripwire(monkeypatch)
    harvested = _harvest_recorder(monkeypatch)
    # Not opted into auto-recover → keep the composite cluster-free.
    monkeypatch.setattr(
        monitor_flow_module,
        "maybe_resolve_and_recover",
        lambda experiment_dir, run_id, **_kw: ResolveAndRecoverOutcome(run_id=run_id),
    )
    _stub_census(monkeypatch, _present(1, 3))

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.FAILED
    assert walk == []
    assert harvested == ["failed"]


def test_partial_census_rides_to_timeout_when_scheduler_unprobeable(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PARTIAL census whose scheduler liveness cross-check is UNAVAILABLE must
    never settle terminal: the missing tasks read as pending, so the run rides to
    the wall-clock budget (TIMEOUT) with no reporter walk. The autouse
    ``_no_census_scheduler_probes`` fixture makes ``_census_liveness_probe``
    return ``None`` (fail closed), so the F17 remap cannot fire — the historical
    ride-to-budget behavior is preserved exactly when the scheduler cannot be
    reached."""
    _seed_record(experiment)
    walk = _walk_tripwire(monkeypatch)
    harvested = _harvest_recorder(monkeypatch)
    # 2 complete, 0 failed, 2 still missing → pending=2 → in flight (probe unavailable).
    _stub_census(monkeypatch, _present(2, 0))

    clock = {"t": 0.0}
    result = monitor_flow(
        experiment,
        spec=_spec(wall_clock_budget_seconds=12),
        _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        _now=lambda: clock["t"],
    )

    assert result.lifecycle_state == LifecycleState.TIMEOUT
    assert walk == []  # census-only, even mid-flight
    assert harvested == ["cap-overrun"]
    progress = result.last_status.get("task_announcements")
    assert progress == {"announced": 2, "complete": 2, "failed": 0, "missing": 2}


def test_partial_static_census_settles_abandoned_when_scheduler_empty(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F17 fire path: a partial census that has gone STATIC while the scheduler
    holds NOTHING alive means the missing tasks died without ever announcing
    (preemption handler / SIGKILL / node crash). The liveness cross-check re-maps
    the missing bucket to UNKNOWN so the bounded-unknown watchdog settles the run
    ABANDONED — instead of riding the whole budget to TIMEOUT with the bumped
    tasks never resubmitted."""
    _seed_record(experiment)
    walk = _walk_tripwire(monkeypatch)
    harvested = _harvest_recorder(monkeypatch)
    # 2 complete, 0 failed, 2 missing forever (the 2 bumped tasks never announce).
    _stub_census(monkeypatch, _present(2, 0))
    # The scheduler ran clean and holds NOTHING alive for this run's job ids.
    probes: list[int] = []

    def _empty_probe(record: Any) -> set[str]:
        probes.append(1)
        return set()

    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", _empty_probe)

    clock = {"t": 0.0}
    result = monitor_flow(
        experiment,
        # Budget generous enough to reach the ABANDONED escalation before TIMEOUT.
        spec=_spec(wall_clock_budget_seconds=100_000),
        _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        _now=lambda: clock["t"],
    )

    assert result.lifecycle_state == LifecycleState.ABANDONED
    assert walk == []  # census-only; the fix is a scheduler probe, not a walk
    assert harvested == [str(LifecycleState.ABANDONED)]
    assert probes  # the liveness cross-check actually fired
    # The provenance marks that the census was corrected by a scheduler probe.
    assert result.last_status.get("status_source") == "task_announcements+scheduler_liveness"


def test_partial_static_census_stays_in_flight_when_scheduler_alive(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F17 guard: when the scheduler still holds the run's jobs ALIVE, a static
    partial census must NOT escalate — the missing tasks are genuinely still
    queued/running. The run rides to TIMEOUT (bounded), never a false ABANDONED."""
    _seed_record(experiment)
    harvested = _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(2, 0))
    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", lambda record: {"9001"})

    clock = {"t": 0.0}
    result = monitor_flow(
        experiment,
        spec=_spec(wall_clock_budget_seconds=12),
        _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        _now=lambda: clock["t"],
    )

    assert result.lifecycle_state == LifecycleState.TIMEOUT
    assert harvested == ["cap-overrun"]


def test_failed_census_stays_in_flight_when_resubmit_alive(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F23 fire path: a FULL census reads complete+failed==total (would settle
    FAILED), but the scheduler POSITIVELY holds a resubmitted attempt still alive
    — the .failed markers are stale (a resubmit never cleared them). The census
    must NOT settle terminal, so auto-resume/recover cannot thrash duplicate
    arrays; the run stays in-flight (bounded by the budget)."""
    _seed_record(experiment)
    harvested = _harvest_recorder(monkeypatch)
    # 1 complete + 3 failed == 4 total, missing=0 → classify_polling => FAILED.
    _stub_census(monkeypatch, _present(1, 3))
    probes: list[int] = []

    def _alive_probe(record: Any) -> set[str]:
        probes.append(1)
        return {"9001"}  # attempt-2 array still queued/running

    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", _alive_probe)

    clock = {"t": 0.0}
    result = monitor_flow(
        experiment,
        spec=_spec(wall_clock_budget_seconds=12),
        _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        _now=lambda: clock["t"],
    )

    # Never settled FAILED — the stale markers did not win over live scheduler
    # evidence. The run rides to the (bounded) budget instead.
    assert result.lifecycle_state == LifecycleState.TIMEOUT
    assert harvested == ["cap-overrun"]
    assert probes  # the cross-check fired at the would-be-FAILED settle


def test_failed_census_still_settles_when_scheduler_empty(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F23 regression: a genuine failure (census FAILED, scheduler holds NOTHING
    alive) still settles FAILED. The cross-check acts only on POSITIVE liveness
    evidence, so a dead queue falls through to the normal settle unchanged."""
    _seed_record(experiment)
    harvested = _harvest_recorder(monkeypatch)
    monkeypatch.setattr(
        monitor_flow_module,
        "maybe_resolve_and_recover",
        lambda experiment_dir, run_id, **_kw: ResolveAndRecoverOutcome(run_id=run_id),
    )
    _stub_census(monkeypatch, _present(1, 3))
    monkeypatch.setattr(monitor_flow_module, "_census_liveness_probe", lambda record: set())

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.FAILED
    assert harvested == ["failed"]


def _write_wave_sidecar(experiment_dir: Path, wave_map: dict[str, list[int]]) -> None:
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.26",
        submitted_at="2026-07-12T09:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="1" * 64,
        wave_map=wave_map,
    )


def test_census_leg_combines_waves_from_markers(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F28 fire path: on the census leg, ``auto_combine_waves`` fires by deriving
    a per-wave completeness block from the announce complete-markers — the census
    ``last_status`` otherwise carries no ``waves`` block, so combines silently
    no-op for announce-era runs and the terminal harvest has nothing combined."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment, {"0": [0, 1], "1": [2, 3]})
    _walk_tripwire(monkeypatch)
    _harvest_recorder(monkeypatch)
    # Full-complete census (all 4 announced complete) → both waves complete.
    _stub_census(monkeypatch, _present(4, 0))
    # The complete-marker listing returns every completed task id.
    monkeypatch.setattr(
        monitor_flow_module, "_census_complete_task_ids", lambda record, run_id: {0, 1, 2, 3}
    )
    combined: list[int] = []

    def _fake_combine(
        experiment_dir: Path, run_id: str, *, waves: list[int], **_kw: Any
    ) -> dict[int, tuple[bool, str, str]]:
        combined.extend(waves)
        return {w: (True, "", "") for w in waves}

    monkeypatch.setattr(monitor_flow_module, "combine_waves", _fake_combine)

    result = monitor_flow(
        experiment,
        spec=_spec(auto_combine_waves=True),
        _sleep=lambda s: None,
        _now=lambda: 0.0,
    )

    assert result.lifecycle_state == LifecycleState.COMPLETE
    # BOTH waves were combined from the census markers — the F28 no-op is gone.
    assert sorted(combined) == [0, 1]
    assert result.combined_waves == [0, 1]


def test_census_leg_wave_bookkeeping_degrades_when_listing_unavailable(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F28 fail-closed: when the complete-marker listing cannot run (``None``),
    the census leg degrades to NO wave bookkeeping this tick rather than
    combining on a guessed block — the run still settles, just with no combine."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment, {"0": [0, 1], "1": [2, 3]})
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(4, 0))
    # Autouse fixture already defaults _census_complete_task_ids -> None; be explicit.
    monkeypatch.setattr(
        monitor_flow_module, "_census_complete_task_ids", lambda record, run_id: None
    )
    combined: list[int] = []

    def _fake_combine(
        experiment_dir: Path, run_id: str, *, waves: list[int], **_kw: Any
    ) -> dict[int, tuple[bool, str, str]]:
        combined.extend(waves)
        return {w: (True, "", "") for w in waves}

    monkeypatch.setattr(monitor_flow_module, "combine_waves", _fake_combine)

    result = monitor_flow(
        experiment,
        spec=_spec(auto_combine_waves=True),
        _sleep=lambda s: None,
        _now=lambda: 0.0,
    )

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert combined == []  # no waves block derived → no combine attempted


def test_ten_wave_burst_is_one_combine_exec(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P4 tier-1 acceptance: a tick that finds a 10-wave BURST fires ``combine_waves``
    exactly ONCE (one fused exec) with all ten waves — not ten serial calls — and
    each wave lands in ``combined_waves`` (per-wave accounting)."""
    wave_map = {str(w): [2 * w, 2 * w + 1] for w in range(10)}  # 10 waves, 20 tasks
    _seed_record(experiment, total_tasks=20, job_ids=["1"])
    _write_wave_sidecar_n(experiment, wave_map, task_count=20)
    _walk_tripwire(monkeypatch)
    _harvest_recorder(monkeypatch)
    # Full-complete census (all 20 announced complete) → all ten waves complete.
    _stub_census(monkeypatch, _present(20, 0, total=20))
    monkeypatch.setattr(
        monitor_flow_module,
        "_census_complete_task_ids",
        lambda record, run_id: set(range(20)),
    )

    calls: list[list[int]] = []

    def _fake_combine(
        experiment_dir: Path, run_id: str, *, waves: list[int], **_kw: Any
    ) -> dict[int, tuple[bool, str, str]]:
        calls.append(list(waves))
        return {w: (True, "", "") for w in waves}

    monkeypatch.setattr(monitor_flow_module, "combine_waves", _fake_combine)

    result = monitor_flow(
        experiment,
        spec=_spec(auto_combine_waves=True),
        _sleep=lambda s: None,
        _now=lambda: 0.0,
    )

    assert result.lifecycle_state == LifecycleState.COMPLETE
    # ONE fused combine exec for the whole burst — the head-of-line stall is gone.
    assert len(calls) == 1
    assert sorted(calls[0]) == list(range(10))
    assert result.combined_waves == list(range(10))


def _write_wave_sidecar_n(
    experiment_dir: Path, wave_map: dict[str, list[int]], *, task_count: int
) -> None:
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.26",
        submitted_at="2026-07-12T09:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{task_id}",
        task_count=task_count,
        tasks_py_sha="1" * 64,
        wave_map=wave_map,
    )


def test_pre_announce_falls_back_to_walk_disclosed(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PRE-ANNOUNCE run (no announce dir → not-present census) falls back to the
    reporter walk, DISCLOSED in-band via ``status_source``."""
    _seed_record(experiment)
    harvested = _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _ABSENT)  # never present

    # The walk reports a clean COMPLETE so the run settles via the pull leg.
    from hpc_agent.state.journal import update_run_status

    def _walk(experiment_dir: Path, run_id: str, **_kw: Any) -> RunRecord:
        return update_run_status(
            experiment_dir,
            run_id,
            last_status={"complete": 4, "running": 0, "pending": 0, "failed": 0},
        )

    monkeypatch.setattr(monitor_flow_module, "record_status", _walk)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert harvested == ["complete"]
    # The fallback disclosed itself in-band on the tick's status snapshot.
    assert result.last_status.get("status_source") == "status_reporter_walk"


def test_mixed_walks_until_first_marker_then_census(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that starts pre-announce (no marker yet) walks; once the dispatcher
    writes its first terminal marker the census becomes present and TAKES OVER —
    the walk runs exactly once, then the full census settles the run."""
    _seed_record(experiment)
    harvested = _harvest_recorder(monkeypatch)
    # Tick 1: not present → walk. Tick 2+: present & full-complete → census.
    _stub_census(monkeypatch, _ABSENT, _present(4, 0))

    from hpc_agent.state.journal import update_run_status

    walk_calls: list[str] = []

    def _walk(experiment_dir: Path, run_id: str, **_kw: Any) -> RunRecord:
        walk_calls.append(run_id)
        # Report an in-flight snapshot so tick 1 does NOT settle on the walk.
        return update_run_status(
            experiment_dir,
            run_id,
            last_status={"complete": 0, "running": 4, "pending": 0, "failed": 0},
        )

    monkeypatch.setattr(monitor_flow_module, "record_status", _walk)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    # The walk ran exactly once (tick 1); the census took over from tick 2.
    assert walk_calls == [_RUN_ID]
    assert result.last_status.get("status_source") == "task_announcements"
    assert harvested == ["complete"]
