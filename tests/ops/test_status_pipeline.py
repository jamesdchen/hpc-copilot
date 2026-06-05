"""Tests for the ``status-pipeline`` composite.

The pipeline composes one workflow atom (``monitor-flow``) and branches on the
returned ``lifecycle_state``; these tests mock ``monitor-flow`` at the
``status_pipeline`` module seam and exercise every ``stage_reached`` path — no
cluster, no journal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

from hpc_agent._wire.workflows.monitor_flow import MonitorFlowResult, MonitorFlowSpec
from hpc_agent._wire.workflows.status_pipeline import StatusPipelineSpec

if TYPE_CHECKING:
    from pathlib import Path


def _pipeline_spec() -> StatusPipelineSpec:
    return StatusPipelineSpec(monitor=MonitorFlowSpec(run_id="ml-abcd1234"))


def _mf_result(lifecycle: str, **kw: Any) -> MonitorFlowResult:
    base: dict[str, Any] = {
        "run_id": "ml-abcd1234",
        "lifecycle_state": lifecycle,
        "last_status": {"complete": 4, "failed": 0, "checked_at": "2026-06-05T00:00:00Z"},
        "combined_waves": [0],
        "failed_waves": [],
        "ticks": 3,
        "elapsed_seconds": 12.5,
        "escalation_reason": None,
    }
    base.update(kw)
    return MonitorFlowResult(**base)


def test_complete_is_clean_terminal(tmp_path: Path) -> None:
    from hpc_agent.ops.status_pipeline import status_pipeline

    with mock.patch(
        "hpc_agent.ops.status_pipeline.monitor_flow", return_value=_mf_result("complete")
    ):
        res = status_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "complete"
    assert res.needs_decision is False
    assert res.lifecycle_state == "complete"
    assert res.combined_waves == [0]
    assert "aggregate" in res.reason


def test_timeout_is_clean_terminal_no_decision(tmp_path: Path) -> None:
    from hpc_agent.ops.status_pipeline import status_pipeline

    with mock.patch(
        "hpc_agent.ops.status_pipeline.monitor_flow", return_value=_mf_result("timeout")
    ):
        res = status_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "timeout"
    assert res.needs_decision is False  # budget elapsed, jobs live — re-invoke, not a decision
    assert "re-invoke" in res.reason


def test_failed_escalates_with_evidence(tmp_path: Path) -> None:
    from hpc_agent.ops.status_pipeline import status_pipeline

    mf = _mf_result(
        "failed",
        failed_waves=[2],
        last_status={"complete": 3, "failed": 1, "checked_at": "2026-06-05T00:00:00Z"},
        escalation_reason="failed_tasks_no_auto_recover_in_mvp",
    )
    with mock.patch("hpc_agent.ops.status_pipeline.monitor_flow", return_value=mf):
        res = status_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "failed"
    assert res.needs_decision is True  # caller classifies + decides resubmit
    assert res.failed_waves == [2]
    assert res.escalation_reason == "failed_tasks_no_auto_recover_in_mvp"
    assert res.last_status["failed"] == 1


def test_abandoned_escalates(tmp_path: Path) -> None:
    from hpc_agent.ops.status_pipeline import status_pipeline

    with mock.patch(
        "hpc_agent.ops.status_pipeline.monitor_flow",
        return_value=_mf_result("abandoned", escalation_reason="abandoned_by_reconcile"),
    ):
        res = status_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "abandoned"
    assert res.needs_decision is True
    assert "reconcile-journal" in res.reason
