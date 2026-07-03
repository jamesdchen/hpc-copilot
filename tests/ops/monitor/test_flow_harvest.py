"""Guaranteed-harvest wiring in ``monitor_flow`` (design §5).

Every terminal path — complete / failed / timeout(cap-overrun) /
abandoned — AND any abnormal loop exit must end in ``harvest_on_terminal``.
These tests drive the poll loop with cluster-free fakes (mirroring the
sibling monitor-flow tests) and assert the guard fires with the right
terminal cause on each path, that the original exception still propagates
on an abnormal exit, and that a durable LOUD marker is written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state import run_record
from hpc_agent.state.journal import update_run_status, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260703-100000-eee"


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
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "myjob",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-07-03T10:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "backend": "slurm",
        "auto_resume_on_kill": False,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _status_stub(experiment: Path, snapshots: list[dict[str, int]]):
    idx = {"n": 0}

    def _fake(experiment_dir: Path, run_id: str, **kwargs: Any) -> RunRecord:
        snap = snapshots[min(idx["n"], len(snapshots) - 1)]
        idx["n"] += 1
        return update_run_status(experiment_dir, run_id, last_status=dict(snap))

    return _fake


def _install_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace monitor_flow.harvest_on_terminal with a call recorder."""
    calls: list[dict[str, Any]] = []

    def _rec(
        experiment_dir: Path,
        run_id: str,
        *,
        terminal_cause: str,
        record: Any = None,
        **k: Any,
    ) -> dict[str, Any]:
        calls.append({"run_id": run_id, "terminal_cause": terminal_cause})
        return {"harvest_ok": True}

    monkeypatch.setattr(monitor_flow_module, "harvest_on_terminal", _rec)
    return calls


_COMPLETE = {"complete": 4, "running": 0, "pending": 0, "failed": 0}
_FAILED = {"complete": 2, "running": 0, "pending": 0, "failed": 2}
_IN_FLIGHT = {"complete": 2, "running": 2, "pending": 0, "failed": 0}


def _spec(**kw: Any) -> MonitorFlowSpec:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "poll_interval_seconds": 5,
        "wall_clock_budget_seconds": 10_000,
        "auto_combine_waves": False,
    }
    base.update(kw)
    return MonitorFlowSpec(**base)


def test_complete_path_harvests(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_record(experiment)
    monkeypatch.setattr(monitor_flow_module, "record_status", _status_stub(experiment, [_COMPLETE]))
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    calls = _install_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert calls == [{"run_id": _RUN_ID, "terminal_cause": "complete"}]


def test_failed_path_harvests(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_record(experiment)
    monkeypatch.setattr(monitor_flow_module, "record_status", _status_stub(experiment, [_FAILED]))
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    calls = _install_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.FAILED
    assert calls == [{"run_id": _RUN_ID, "terminal_cause": "failed"}]


def test_budget_path_harvests_cap_overrun(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-flight run that exceeds the wall-clock budget harvests as cap-overrun."""
    _seed_record(experiment)
    monkeypatch.setattr(
        monitor_flow_module, "record_status", _status_stub(experiment, [_IN_FLIGHT])
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    calls = _install_recorder(monkeypatch)

    # started consumes 0.0; the tick-1 elapsed reads 100.0 >= budget 50.
    now_vals = iter([0.0, 100.0, 100.0])
    result = monitor_flow(
        experiment,
        spec=_spec(wall_clock_budget_seconds=50),
        _sleep=lambda s: None,
        _now=lambda: next(now_vals),
    )

    assert result.lifecycle_state == LifecycleState.TIMEOUT
    assert calls == [{"run_id": _RUN_ID, "terminal_cause": "cap-overrun"}]


def test_abandoned_path_harvests(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-complete/failed terminal verdict (abandoned) flows through harvest.

    ``classify_polling`` does not emit abandoned today, so we force
    ``_is_terminal`` to return it and assert the generic terminal branch
    marks the run terminal and harvests under the ``abandoned`` cause.
    """
    _seed_record(experiment)
    monkeypatch.setattr(
        monitor_flow_module, "record_status", _status_stub(experiment, [_IN_FLIGHT])
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    monkeypatch.setattr(
        monitor_flow_module,
        "_is_terminal",
        lambda *a, **k: (LifecycleState.ABANDONED, "no_on_disk_evidence"),
    )
    calls = _install_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.ABANDONED
    assert result.escalation_reason == "no_on_disk_evidence"
    assert calls == [{"run_id": _RUN_ID, "terminal_cause": "abandoned"}]
    # The journal was marked terminal (abandoned), same as a clean branch.
    refreshed = monitor_flow_module.load_run(experiment, _RUN_ID)
    assert refreshed is not None
    assert refreshed.status == "abandoned"


def test_abnormal_exit_harvests_and_reraises(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception inside the poll loop: the finally harvests under the
    abnormal-exit sentinel, writes a durable LOUD marker, and the ORIGINAL
    exception propagates (the harvest never masks it).

    Uses the REAL harvest_on_terminal (cluster-free via the conftest seams)
    so we can assert the marker actually landed on disk.
    """
    _seed_record(experiment)

    class _Boom(RuntimeError):
        pass

    def _boom_status(experiment_dir: Path, run_id: str, **kwargs: Any) -> RunRecord:
        raise _Boom("cluster reporter died mid-poll")

    monkeypatch.setattr(monitor_flow_module, "record_status", _boom_status)

    with pytest.raises(_Boom, match="died mid-poll"):
        monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=lambda: 0.0)

    # A durable harvest marker was written under the abnormal-exit sentinel.
    path = harvest_marker_path(experiment, _RUN_ID)
    assert path.exists()
    import json

    markers = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(markers) == 1
    assert markers[0]["terminal_cause"] == "abnormal-exit"
    assert markers[0]["run_id"] == _RUN_ID
