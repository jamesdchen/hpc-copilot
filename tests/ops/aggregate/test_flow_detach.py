"""Detach-by-contract seam on the ``aggregate-flow`` atom (design §3; run-#10 F-K).

aggregate-flow is a COMPOSED atom, so ``detach`` defaults OFF (harvest-guard's §5
guaranteed harvest, submit-s4, aggregate-run, campaign-run all call it
synchronously). Detach is OPT-IN for a DIRECT top-level invocation (the MCP seam
forces an agent to pass detach=true). These pin:

* detach=false (default) runs the in-process reduce (``_aggregate_flow_impl``);
* detach=true spawns a durable worker and returns a {started, watch, pid} handle,
  child spec carries detach=false;
* gate → detach ordering: a missing journal record raises JournalCorrupt in the
  PARENT, before any spawn;
* a re-invoke after the worker finished REPLAYS the recorded terminal (no respawn).

Cluster-free: the launcher and the in-process reduce are patched at the module
boundary, so nothing is spawned and no SSH runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

import hpc_agent.ops.aggregate_flow as af
from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops.aggregate_flow import AggregateFlowResult
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260703-120000-agg"
_LAUNCH_PATH = "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"


class _FakeLaunch:
    run_id = _RUN_ID
    pid = 4242
    log_path = "/x/detached.log"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _record(**overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "p",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-07-03T12:00:00+00:00",
        "experiment_dir": "/tmp/exp",
        "status": "complete",
    }
    base.update(overrides)
    return RunRecord(**base)


def _flow_result(**overrides: Any) -> AggregateFlowResult:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "combined_waves": [0, 1],
        "failed_waves": [],
        "waves_combined_this_call": [0, 1],
        "combiner_dir_local": "/tmp/agg/_combiner",
        "aggregated_metrics": {"ridge_h5": {"rmse": 0.12}},
    }
    base.update(overrides)
    return AggregateFlowResult(**base)


def _sidecar(experiment: Path, *, cmd_sha: str = "deadbeef") -> None:
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="",
    )


def test_flow_default_detach_off_runs_in_process(journal_home, experiment) -> None:
    """The composed default: detach OFF → the in-process reduce runs, no spawn."""
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        mock.patch.object(af, "_aggregate_flow_impl", return_value=_flow_result()) as m_impl,
    ):
        result = af.aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    m_launch.assert_not_called()
    m_impl.assert_called_once()
    assert result.started is False
    assert result.aggregated_metrics == {"ridge_h5": {"rmse": 0.12}}


def test_flow_detach_true_spawns_and_returns_handle(journal_home, experiment) -> None:
    upsert_run(experiment, _record())
    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(af, "_aggregate_flow_impl") as m_impl,
    ):
        result = af.aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID, detach=True))

    m_impl.assert_not_called()
    m_launch.assert_called_once()
    assert m_launch.call_args.kwargs["verb"] == "aggregate-flow"
    assert m_launch.call_args.kwargs["spec"]["detach"] is False
    assert result.started is True
    assert result.watch == "journal"
    assert result.detached_pid == 4242


def test_flow_detach_missing_record_raises_before_spawn(journal_home, experiment) -> None:
    """gate → detach ordering: no journal record → JournalCorrupt in the PARENT,
    the launcher is never reached."""
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        pytest.raises(errors.JournalCorrupt, match="no journal record"),
    ):
        af.aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID, detach=True))
    m_launch.assert_not_called()


def test_flow_replays_recorded_terminal_without_respawn(journal_home, experiment) -> None:
    """A synchronous reduce (what the detached child runs) records its terminal;
    a detach=true re-invoke replays it — launcher never called, impl never re-run."""
    upsert_run(experiment, _record())
    _sidecar(experiment)

    # 1. The child runs synchronously (detach off) and records its terminal.
    with mock.patch.object(af, "_aggregate_flow_impl", return_value=_flow_result()):
        sync = af.aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))
    assert sync.aggregated_metrics == {"ridge_h5": {"rmse": 0.12}}

    # 2. The parent's re-invoke (detach on) replays it — no spawn, no re-reduce.
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        mock.patch.object(af, "_aggregate_flow_impl") as m_impl,
    ):
        replay = af.aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID, detach=True))

    m_launch.assert_not_called()
    m_impl.assert_not_called()
    assert replay.aggregated_metrics == sync.aggregated_metrics
    assert replay.combined_waves == [0, 1]
