"""Precondition gates on monitor-flow and aggregate-flow.

A workflow step invoked out of order must fail loud with
``precondition_failed`` before any cluster-side work, rather than
proceed on a stale assumption and loop against nothing / reduce over
partial data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._internal import session
from hpc_agent._internal.session import RunRecord, run_record
from hpc_agent._schema_models.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent._schema_models.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.flows.monitor_flow import monitor_flow
from hpc_agent.ops.aggregate.flow import aggregate_flow

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260521-120000-aaa"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    monkeypatch.setattr(session, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _record(**overrides) -> RunRecord:
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
        "experiment_dir": "/tmp/exp",
    }
    base.update(overrides)
    return RunRecord(**base)


def test_monitor_flow_rejects_run_with_no_job_ids(journal_home, experiment):
    session.upsert_run(experiment, _record(job_ids=[]))
    with pytest.raises(errors.PreconditionFailed, match="no scheduler job ids"):
        monitor_flow(experiment, spec=MonitorFlowSpec(run_id=_RUN_ID))


def test_monitor_flow_unknown_run_is_journal_corrupt(journal_home, experiment):
    with pytest.raises(errors.JournalCorrupt):
        monitor_flow(experiment, spec=MonitorFlowSpec(run_id="no-such-run"))


def test_aggregate_flow_rejects_non_terminal_run(journal_home, experiment):
    session.upsert_run(experiment, _record(status="in_flight"))
    with pytest.raises(errors.PreconditionFailed, match="not terminal"):
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))


def test_aggregate_flow_partial_opt_in_bypasses_terminal_gate(journal_home, experiment):
    # ensure_all_combined=false is the documented partial-aggregate
    # opt-in and must bypass the terminal-state gate. The empty
    # ssh_target makes the call fail fast at ssh validation — a step
    # AFTER the gate — which proves the gate itself did not fire.
    session.upsert_run(experiment, _record(status="in_flight", ssh_target=""))
    spec = AggregateFlowSpec(run_id=_RUN_ID, ensure_all_combined=False)
    with pytest.raises(errors.HpcError) as exc_info:
        aggregate_flow(experiment, spec=spec)
    assert not isinstance(exc_info.value, errors.PreconditionFailed)


def test_aggregate_flow_allows_terminal_run_past_gate(journal_home, experiment):
    # A terminal run passes the gate; the empty ssh_target then fails it
    # at ssh validation — again, anything but PreconditionFailed proves
    # the gate let a terminal run through.
    session.upsert_run(experiment, _record(status="complete", ssh_target=""))
    with pytest.raises(errors.HpcError) as exc_info:
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))
    assert not isinstance(exc_info.value, errors.PreconditionFailed)
