"""Poll-loop fault tolerance for ``monitor_flow`` (SSH#1).

A single transient poll fault — a reporter rc!=0 (``RemoteCommandFailed``) or a
``TimeoutError`` (an ``OSError`` subclass) after the backoff window — must NOT
abort a healthy multi-hour poll. The per-tick ``record_status`` call is wrapped
so the loop swallows the blip, notes it on a tick, and continues to the next
poll; only a poller that keeps failing PAST the wall-clock budget terminates —
to ``TIMEOUT`` with the guaranteed harvest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state import run_record
from hpc_agent.state.journal import update_run_status, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260606-130000-ccc"
_COMPLETE_STATUS = {"complete": 4, "running": 0, "pending": 0, "failed": 0}


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
        "submitted_at": "2026-06-06T13:00:00+00:00",
        "experiment_dir": str(experiment_dir),
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _record_status_sequence(experiment: Path, seq: list[Any]):
    """A record_status stub that walks *seq*: a dict is persisted as
    ``last_status`` and returned; an Exception is raised (a transient fault).
    The last item repeats once the sequence is exhausted."""
    idx = {"n": 0}

    def _fake(experiment_dir: Path, run_id: str, **kwargs: Any) -> RunRecord:
        item = seq[min(idx["n"], len(seq) - 1)]
        idx["n"] += 1
        if isinstance(item, BaseException):
            raise item
        return update_run_status(experiment_dir, run_id, last_status=dict(item))

    return _fake


@pytest.mark.parametrize(
    "transient",
    [errors.RemoteCommandFailed("reporter rc=1"), TimeoutError("ssh timed out")],
    ids=["remote_command_failed", "timeout_error"],
)
def test_single_transient_does_not_abort_poll(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient: BaseException,
) -> None:
    """A single transient poll fault is swallowed; the loop continues to the
    next poll and reaches COMPLETE (and still harvests)."""
    _seed_record(experiment)
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _record_status_sequence(experiment, [transient, _COMPLETE_STATUS]),
    )
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
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert harvested == ["complete"]


def test_repeated_transient_terminates_to_timeout(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poller that keeps failing does NOT spin forever — it stays bounded by
    the wall-clock budget and terminates to TIMEOUT with a guaranteed harvest."""
    _seed_record(experiment)
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _record_status_sequence(experiment, [errors.RemoteCommandFailed("always down")]),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    harvested: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: harvested.append(k.get("terminal_cause", "?")),
    )

    # Virtual clock: _now reads it, _sleep advances it, so repeated failed polls
    # cross the budget in finite iterations.
    clock = {"t": 0.0}
    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10,
        auto_combine_waves=False,
    )
    result = monitor_flow(
        experiment,
        spec=spec,
        _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        _now=lambda: clock["t"],
    )

    assert result.lifecycle_state == LifecycleState.TIMEOUT
    assert harvested == ["cap-overrun"]
