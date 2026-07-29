"""Wave-incremental harvest prefetch — the WATCH-side trigger.

The monitor poll loop already combines each wave the moment its tasks are all
terminal (``auto_combine_waves``); this seam pins the NEW step layered on it:
after a combine BURST succeeds, the watch opportunistically pulls the sealed
``_combiner`` wave partials into the terminal harvest's default destination —
overlapping the transfer with the still-running later waves — so the terminal
harvest's pull transfers only the delta.

Cluster-etiquette + correctness contract pinned here:

* at most ONE prefetch pull per combine burst, triggered solely by the
  wave-completion state the watch already reads — NEVER a per-poll round-trip;
* an incomplete (uncombined) wave NEVER triggers a prefetch;
* a prefetch transport failure is disclosed on the tick action row and never
  disturbs the watch (the run still settles normally);
* ``HPC_WAVE_PREFETCH=0`` opts out entirely.

The census-leg fixtures mirror ``test_flow_announce.py`` (the F28 wave
bookkeeping this trigger piggybacks on).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260729-110000-pref"


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
        "submitted_at": "2026-07-29T09:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "backend": "sge",
        "auto_resume_on_kill": False,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _write_wave_sidecar(experiment_dir: Path, wave_map: dict[str, list[int]]) -> None:
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
    )


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
    idx = {"n": 0}

    def _fake(*, ssh_target: str, remote_path: str, run_id: str, task_count: int) -> dict[str, int]:
        item = census_seq[min(idx["n"], len(census_seq) - 1)]
        idx["n"] += 1
        return dict(item)

    monkeypatch.setattr(monitor_flow_module, "read_announcements", _fake)


def _present(complete: int, failed: int = 0, total: int = 4) -> dict[str, int]:
    announced = complete + failed
    return {
        "present": 1,
        "announced": announced,
        "complete": complete,
        "failed": failed,
        "missing": max(0, total - announced),
    }


def _stub_complete_ids(monkeypatch: pytest.MonkeyPatch, *id_seq: set[int]) -> None:
    idx = {"n": 0}

    def _fake(record: Any, run_id: str) -> set[int]:
        item = id_seq[min(idx["n"], len(id_seq) - 1)]
        idx["n"] += 1
        return set(item)

    monkeypatch.setattr(monitor_flow_module, "_census_complete_task_ids", _fake)


def _ok_combine(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    calls: list[list[int]] = []

    def _fake(
        experiment_dir: Path, run_id: str, *, waves: list[int], **_kw: Any
    ) -> dict[int, tuple[bool, str, str]]:
        calls.append(list(waves))
        return {w: (True, "", "") for w in waves}

    monkeypatch.setattr(monitor_flow_module, "combine_waves", _fake)
    return calls


def _prefetch_pull_recorder(
    monkeypatch: pytest.MonkeyPatch, *, raise_exc: Exception | None = None
) -> list[dict[str, Any]]:
    """Record the aggregate-side ``_pull`` the prefetch drives (engine-agnostic)."""
    calls: list[dict[str, Any]] = []

    def _fake(**kw: Any) -> Any:
        calls.append(kw)
        if raise_exc is not None:
            raise raise_exc
        return af_module._PullOutcome(
            returncode=0, stderr="", files_pulled=2, bytes_pulled=2048, skipped_unchanged=0
        )

    monkeypatch.setattr(af_module, "_pull", _fake)
    return calls


def _spec(**overrides: Any) -> MonitorFlowSpec:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "poll_interval_seconds": 5,
        "wall_clock_budget_seconds": 10_000,
        "auto_combine_waves": True,
    }
    base.update(overrides)
    return MonitorFlowSpec(**base)


def _prefetch_actions(experiment_dir: Path) -> list[dict[str, Any]]:
    """The ``prefetch_wave_partials`` action rows from the run's tick log."""
    from hpc_agent.ops.monitor.tick_log import _tick_log_path

    rows: list[dict[str, Any]] = []
    log = _tick_log_path(experiment_dir, _RUN_ID)
    if not log.is_file():
        return rows
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        tick = json.loads(line)
        for action in tick.get("actions") or []:
            if isinstance(action, dict) and action.get("kind") == "prefetch_wave_partials":
                rows.append(action)
    return rows


def test_prefetch_fires_once_per_burst_with_the_harvest_pull_shape(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick whose burst combines both waves fires exactly ONE prefetch pull —
    the harvest's own (subdir, include, destination) triple — and records the
    disclosed action row on the tick log."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment, {"0": [0, 1], "1": [2, 3]})
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(4))
    _stub_complete_ids(monkeypatch, {0, 1, 2, 3})
    combine_calls = _ok_combine(monkeypatch)
    pulls = _prefetch_pull_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert combine_calls == [[0, 1]]
    assert len(pulls) == 1  # ONE pull for the whole burst, not one per wave
    kw = pulls[0]
    assert kw["remote_subdir"] == "_combiner"
    assert kw["include"] == list(af_module._WAVE_PARTIAL_INCLUDE)
    assert kw["local_dir"] == str(experiment / "_aggregated" / _RUN_ID / "_combiner")

    actions = _prefetch_actions(experiment)
    assert len(actions) == 1
    assert actions[0]["waves"] == [0, 1]
    assert actions[0]["ok"] is True
    assert actions[0]["files_pulled"] == 2


def test_incomplete_wave_never_prefetched_and_no_per_poll_round_trips(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three ticks: wave 0 completes (tick 1), NOTHING new (tick 2), wave 1
    completes (tick 3). The prefetch fires exactly twice — once per combine
    burst — and never on the no-progress tick (no per-poll SSH) nor for a wave
    that has not terminally completed."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment, {"0": [0, 1], "1": [2, 3]})
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(2), _present(2), _present(4))
    _stub_complete_ids(monkeypatch, {0, 1}, {0, 1}, {0, 1, 2, 3})
    combine_calls = _ok_combine(monkeypatch)
    pulls = _prefetch_pull_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert combine_calls == [[0], [1]]  # wave 1 combined only once complete
    assert len(pulls) == 2  # burst-triggered; the static middle tick pulled nothing
    actions = _prefetch_actions(experiment)
    assert [a["waves"] for a in actions] == [[0], [1]]


def test_failed_combine_does_not_prefetch(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst whose combine FAILED has no sealed partial to prefetch — the pull
    must not fire on combine failure."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment, {"0": [0, 1], "1": [2, 3]})
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(4))
    _stub_complete_ids(monkeypatch, {0, 1, 2, 3})
    pulls = _prefetch_pull_recorder(monkeypatch)

    def _fail_combine(
        experiment_dir: Path, run_id: str, *, waves: list[int], **_kw: Any
    ) -> dict[int, tuple[bool, str, str]]:
        return {w: (False, "", "combiner exploded") for w in waves}

    monkeypatch.setattr(monitor_flow_module, "combine_waves", _fail_combine)

    result = monitor_flow(
        experiment,
        spec=_spec(combiner_max_retries=0),
        _sleep=lambda s: None,
        _now=lambda: 0.0,
    )

    assert result.lifecycle_state == LifecycleState.COMPLETE  # tasks all complete
    assert result.failed_waves == [0, 1]
    assert pulls == []  # nothing sealed -> nothing prefetched
    assert _prefetch_actions(experiment) == []


def test_prefetch_failure_is_disclosed_and_never_disturbs_the_watch(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport fault inside the prefetch lands as an ``ok=False`` action row;
    the watch still settles COMPLETE with the combine bookkeeping intact."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment, {"0": [0, 1], "1": [2, 3]})
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(4))
    _stub_complete_ids(monkeypatch, {0, 1, 2, 3})
    _ok_combine(monkeypatch)
    _prefetch_pull_recorder(monkeypatch, raise_exc=errors.SshUnreachable("vpn dropped"))

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert result.combined_waves == [0, 1]
    actions = _prefetch_actions(experiment)
    assert len(actions) == 1
    assert actions[0]["ok"] is False
    assert "vpn dropped" in actions[0]["error"]


def test_env_opt_out_disables_the_prefetch(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_record(experiment)
    _write_wave_sidecar(experiment, {"0": [0, 1], "1": [2, 3]})
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(4))
    _stub_complete_ids(monkeypatch, {0, 1, 2, 3})
    _ok_combine(monkeypatch)
    pulls = _prefetch_pull_recorder(monkeypatch)
    monkeypatch.setenv(af_module.WAVE_PREFETCH_ENV, "0")

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert pulls == []
    assert _prefetch_actions(experiment) == []
