"""Completion-aware backoff ceiling: the adaptive cap tightens near expected end.

``monitor_flow``'s adaptive backoff (see ``test_flow_adaptive_poll.py``) doubled
the sleep to a flat 300s cap regardless of how close the job was to its own
requested walltime — a quiet job's terminal could sit unnoticed up to 300s.
The ceiling now clamps the doubled sleep to
``clamp(remaining_expected / 4, poll floor, _MAX_ADAPTIVE_POLL_SECONDS)`` where
``remaining_expected`` counts down from
``running_since + walltime_sec × wave_bound`` — all data the loop already holds
(sidecar ``resources.walltime_sec``, one read at loop start; execution evidence
from the per-tick ``last_status``). Zero new I/O, strictly fewer/never-later
polls.

Pins here:

* a quiet run near its expected end sleeps BELOW the flat cap and reaches the
  floor as remaining → 0;
* unknown walltime (or a job never observed running) keeps today's ramp
  byte-identical (the exact 60→120→240→300 sequence);
* a fingerprint change still snaps instantly to the floor;
* the estimate is a SLEEP bound only — it never touches lifecycle_state or
  escalation (behavioral + source-level pin);
* the ``backoff_ceiling`` tick-action disclosure appears exactly once, and
  only when the ceiling actually bound.

Same seams as the sibling adaptive-poll battery: injected ``_sleep`` / ``_now``,
stubbed ``record_status`` returning fixed snapshots.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import _MAX_ADAPTIVE_POLL_SECONDS, monitor_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord, runs_dir
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260729-090000-ceil"
_POLL_FLOOR = 60.0


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
        "submitted_at": "2026-07-29T09:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "backend": "sge",
        # In-flight AND observably executing (running > 0) so the completion
        # clock arms on tick 1; constant across polls so the fingerprint
        # repeats and the backoff engages.
        "last_status": {
            "complete": 2,
            "running": 2,
            "pending": 0,
            "failed": 0,
            "checked_at": "2026-07-29T09:00:00+00:00",
        },
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _seed_sidecar(
    experiment_dir: Path,
    *,
    resources: dict[str, Any] | None,
    wave_map: dict[str, list[int]] | None = None,
) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.26",
        submitted_at="2026-07-29T09:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="1" * 64,
        wave_map=wave_map,
        resources=resources,
    )


class _Stop(Exception):
    """Sentinel to exit the poll loop after exactly N sleeps."""


def _drive(
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: RunRecord,
    target_polls: int,
    snapshots: list[dict[str, Any]] | None = None,
) -> list[float]:
    """Run monitor_flow with injected clock/sleep; return the recorded sleeps.

    ``snapshots`` (optional) drives a per-poll status sequence; the default is
    the seed record's constant status (fingerprint repeats every tick).
    """

    def _fake_record_status(experiment_dir: Path, run_id: str, **kwargs: Any) -> RunRecord:
        if snapshots is None:
            return seed
        snap = snapshots[min(call_idx["n"], len(snapshots) - 1)]
        call_idx["n"] += 1
        return RunRecord(
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
            backend=seed.backend,
            last_status=dict(snap),
        )

    call_idx = {"n": 0}
    monkeypatch.setattr(monitor_flow_module, "record_status", _fake_record_status)
    monkeypatch.setattr(monitor_flow_module, "mark_terminal", lambda *a, **k: seed)

    sleeps: list[float] = []
    fake_clock = {"t": 0.0}

    def _sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        fake_clock["t"] += float(seconds)
        if len(sleeps) >= target_polls:
            raise _Stop

    def _now() -> float:
        return fake_clock["t"]

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=_POLL_FLOOR,
        wall_clock_budget_seconds=10_000_000,
        auto_combine_waves=False,
    )
    with pytest.raises(_Stop):
        monitor_flow(experiment, spec=spec, _sleep=_sleep, _now=_now)
    return sleeps


def _ticks(experiment: Path) -> list[dict[str, Any]]:
    """The run's tick-log records, in order."""
    path = runs_dir(experiment) / f"{_RUN_ID}.monitor.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _tick_actions(experiment: Path) -> list[dict[str, Any]]:
    """All action rows across the run's tick log, in order."""
    actions: list[dict[str, Any]] = []
    for tick in _ticks(experiment):
        actions.extend(tick.get("actions", []))
    return actions


def test_quiet_run_near_expected_end_bounds_below_cap_and_reaches_floor(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """walltime known + running observed: cap = remaining/4, decaying to floor.

    walltime_sec=1200, single wave, running observed at t=0 (tick 1), so
    expected end = 1200 and the trace (K=2, floor 60) is:

      tick 1  t=0      no prior fp                      sleep 60
      tick 2  t=60     unchanged=1 < K                  sleep 60
      tick 3  t=120    remaining=1080 cap=270  min(120·2? no: 60·2=120)  120
      tick 4  t=240    remaining=960  cap=240  min(240, 240)             240
      tick 5  t=480    remaining=720  cap=180  min(480, 180)  BOUND      180
      tick 6  t=660    remaining=540  cap=135                            135
      tick 7  t=795    remaining=405  cap=101.25                         101.25
      tick 8  t=896.25 remaining=303.75 cap=75.9375                      75.9375
      tick 9  t=972.1875 remaining=227.8125 cap→floor                    60
      tick 10 …past-end ticks stay at the floor                          60

    Today's flat-cap trace would have been 60,60,120,240,300,300,… — every
    bound sleep is strictly earlier, never later.
    """
    seed = _seed_record(experiment)
    _seed_sidecar(experiment, resources={"walltime_sec": 1200})

    sleeps = _drive(experiment, monkeypatch, seed=seed, target_polls=10)

    assert sleeps == [
        60.0,
        60.0,
        120.0,
        240.0,
        180.0,
        135.0,
        101.25,
        75.9375,
        60.0,
        60.0,
    ], sleeps
    # Never exceeds the flat cap, and near the end sits strictly below it.
    assert max(sleeps) < _MAX_ADAPTIVE_POLL_SECONDS
    # remaining → 0 ⇒ cadence back at the floor.
    assert sleeps[-1] == _POLL_FLOOR


def test_multiwave_bound_multiplies_walltime_by_wave_count(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two waves × walltime 600 ⇒ the same 1200s expected end as one 1200s wave.

    Discriminates the ×wave_bound term: a single-wave read of walltime=600
    would give tick 4 remaining=360 → cap=90 (sleep 90, not 240).
    """
    seed = _seed_record(experiment)
    _seed_sidecar(
        experiment,
        resources={"walltime_sec": 600},
        wave_map={"0": [0, 1], "1": [2, 3]},
    )

    sleeps = _drive(experiment, monkeypatch, seed=seed, target_polls=5)

    assert sleeps == [60.0, 60.0, 120.0, 240.0, 180.0], sleeps


@pytest.mark.parametrize(
    "resources",
    [None, {}, {"walltime_sec": None}, {"walltime_sec": 0}],
    ids=["no-resources", "empty", "null-walltime", "zero-walltime"],
)
def test_unknown_walltime_keeps_todays_ramp_byte_identical(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
    resources: dict[str, Any] | None,
) -> None:
    """No usable walltime ⇒ the exact legacy 60→60→120→240→300 sequence."""
    seed = _seed_record(experiment)
    _seed_sidecar(experiment, resources=resources)

    sleeps = _drive(experiment, monkeypatch, seed=seed, target_polls=5)

    assert sleeps == [60.0, 60.0, 120.0, 240.0, 300.0], sleeps
    # And no ceiling disclosure ever fires on the legacy ramp (see the
    # dedicated disclosure test for the binding case).
    assert [a for a in _tick_actions(experiment) if a.get("kind") == "backoff_ceiling"] == []


def test_never_observed_running_keeps_todays_ramp_byte_identical(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """walltime known but the job never left the queue ⇒ legacy ramp exactly.

    Queue wait is unbounded, so a pending-only snapshot must not start the
    completion clock (the ceiling would otherwise tighten against a job that
    has not even started).
    """
    seed = _seed_record(
        experiment,
        last_status={
            "complete": 0,
            "running": 0,
            "pending": 4,
            "failed": 0,
            "checked_at": "2026-07-29T09:00:00+00:00",
        },
    )
    _seed_sidecar(experiment, resources={"walltime_sec": 1200})

    sleeps = _drive(experiment, monkeypatch, seed=seed, target_polls=5)

    assert sleeps == [60.0, 60.0, 120.0, 240.0, 300.0], sleeps


def test_fingerprint_change_still_snaps_to_floor(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instant snap-to-floor on any status change is untouched by the ceiling."""
    seed = _seed_record(experiment)
    _seed_sidecar(experiment, resources={"walltime_sec": 1200})

    quiet = {"complete": 2, "running": 2, "pending": 0, "failed": 0}
    snapshots = [
        dict(quiet),
        dict(quiet),
        dict(quiet),
        dict(quiet),
        {"complete": 3, "running": 1, "pending": 0, "failed": 0},  # change!
    ]
    sleeps = _drive(experiment, monkeypatch, seed=seed, target_polls=5, snapshots=snapshots)

    # Ticks 1-4 as in the near-end test; tick 5's change snaps to the floor
    # (60), not to the ceiling value (180) nor the doubled value.
    assert sleeps == [60.0, 60.0, 120.0, 240.0, 60.0], sleeps


def test_estimate_never_touches_lifecycle_state_or_escalation(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EXPIRED estimate polls at the floor forever — it never settles a verdict.

    walltime_sec=120 expires after two floor sleeps; ten more quiet polls must
    all stay lifecycle in_flight (the estimate bounds the sleep, nothing else),
    and the verdict definitions themselves must not read the estimate's inputs
    (source-level pin on the one count→verdict seam).
    """
    seed = _seed_record(experiment)
    _seed_sidecar(experiment, resources={"walltime_sec": 120})

    sleeps = _drive(experiment, monkeypatch, seed=seed, target_polls=12)

    # Past expected end: every backed-off sleep is clamped to the floor —
    # and the loop KEPT POLLING (12 ticks), it did not escalate/settle.
    assert sleeps == [60.0] * 12, sleeps
    ticks = _ticks(experiment)
    assert len(ticks) == 12
    assert all(t["lifecycle_state"] == "in_flight" for t in ticks), [
        t["lifecycle_state"] for t in ticks
    ]

    # Source pin: the shared terminal/escalation definitions (`_is_terminal`
    # is a thin adapter over `classify_polling` — lifecycle-verdicts row 1)
    # never see the ceiling's inputs.
    from hpc_agent.ops.monitor import classify, terminal

    verdict_src = inspect.getsource(classify) + inspect.getsource(terminal)
    for token in ("running_since", "walltime", "_completion_aware_cap"):
        assert token not in verdict_src, token


def test_disclosure_fires_once_and_only_when_the_ceiling_binds(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `backoff_ceiling` action, on the first BOUND tick, carrying the basis."""
    seed = _seed_record(experiment)
    _seed_sidecar(experiment, resources={"walltime_sec": 1200})

    _drive(experiment, monkeypatch, seed=seed, target_polls=10)

    disclosures = [a for a in _tick_actions(experiment) if a.get("kind") == "backoff_ceiling"]
    # One-shot: many ticks bound (ticks 5-10) but the disclosure fires once.
    assert len(disclosures) == 1, disclosures
    d = disclosures[0]
    # First binding tick (see the near-end trace): t=480, remaining=720, cap=180.
    assert d["remaining_expected_seconds"] == 720.0
    assert d["cap_seconds"] == 180.0
    assert d["walltime_sec"] == 1200.0
    assert d["wave_bound"] == 1

    # And the ticks BEFORE the ceiling bound (1-4: floor/doubling chose the
    # sleep) carry no disclosure — it appears only when the ceiling binds.
    for t in _ticks(experiment)[:4]:
        assert all(a.get("kind") != "backoff_ceiling" for a in t.get("actions", [])), t
