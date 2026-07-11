"""Tests for the ``campaign-run`` composite-of-composites.

``campaign-run`` chains three workflow composites — ``submit-pipeline`` →
``status-pipeline`` → ``aggregate-flow`` — and branches on each typed
outcome. These tests mock all three at the ``campaign_run`` module seam and
exercise every ``stage_reached`` path (plus the deduped-proceeds and
timeout-stops branches) — no cluster, no journal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent._wire.workflows.campaign_run import CampaignRunSpec
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.status_pipeline import StatusPipelineResult, StatusPipelineSpec
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent._wire.workflows.submit_pipeline import SubmitPipelineResult, SubmitPipelineSpec
from hpc_agent.ops.aggregate_flow import AggregateFlowResult

if TYPE_CHECKING:
    from pathlib import Path

_SEAM = "hpc_agent.ops.campaign_run"


def _campaign_spec() -> CampaignRunSpec:
    return CampaignRunSpec(
        submit=SubmitPipelineSpec(
            submit=SubmitAndVerifySpec(
                submit=SubmitFlowSpec(
                    profile="ml",
                    cluster="hoffman2",
                    ssh_target="u@h",
                    remote_path="/r",
                    job_name="ml",
                    run_id="ml-abcd1234",
                    total_tasks=4,
                    backend="sge",
                    script=".hpc/templates/cpu_array.sh",
                    job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
                ),
            ),
        ),
        status=StatusPipelineSpec(monitor=MonitorFlowSpec(run_id="ml-abcd1234")),
        aggregate=AggregateFlowSpec(run_id="ml-abcd1234"),
        campaign_id="camp-iter-7",
        # These exercise the SYNCHRONOUS iteration spine; the detach-by-contract
        # path (default ON) is pinned separately in test_campaign_run_detach.py.
        detach=False,
    )


def _sp_result(stage: str, **kw: Any) -> SubmitPipelineResult:
    base: dict[str, Any] = {
        "stage_reached": stage,
        "needs_decision": stage in {"canary_failed", "verify_submitted_failed"},
        "reason": f"submit reached {stage}",
        "run_id": "ml-abcd1234",
        "job_ids": ["123"],
    }
    base.update(kw)
    return SubmitPipelineResult(**base)


def _st_result(lifecycle: str, **kw: Any) -> StatusPipelineResult:
    base: dict[str, Any] = {
        "stage_reached": lifecycle,
        "needs_decision": lifecycle in {"failed", "abandoned"},
        "reason": f"status reached {lifecycle}",
        "run_id": "ml-abcd1234",
        "lifecycle_state": lifecycle,
        "last_status": {"complete": 4, "failed": 0, "checked_at": "2026-06-05T00:00:00Z"},
        "combined_waves": [0],
        "failed_waves": [],
    }
    base.update(kw)
    return StatusPipelineResult(**base)


def _agg_result(**kw: Any) -> AggregateFlowResult:
    """The ops ``aggregate_flow`` returns a dataclass (with
    ``to_envelope_data()``), NOT the wire pydantic model — mock at the seam
    with the real return type so ``campaign_run`` can serialize it.
    """
    base: dict[str, Any] = {
        "run_id": "ml-abcd1234",
        "combined_waves": [0],
        "failed_waves": [],
        "waves_combined_this_call": [0],
        "combiner_dir_local": "/r/_aggregated/ml-abcd1234/_combiner",
        "aggregated_metrics": {"ml-abcd1234": {"qlike": 0.42}},
        "escalation_reason": None,
    }
    base.update(kw)
    return AggregateFlowResult(**base)


# ── submit_failed ────────────────────────────────────────────────────────────


def test_canary_failed_stops_before_monitor(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    with (
        mock.patch(
            f"{_SEAM}.submit_pipeline",
            return_value=_sp_result("canary_failed", job_ids=[]),
        ),
        mock.patch(f"{_SEAM}.status_pipeline") as m_status,
        mock.patch(f"{_SEAM}.aggregate_flow") as m_agg,
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "submit_failed"
    assert res.needs_decision is True
    assert res.campaign_id == "camp-iter-7"
    assert res.lifecycle_state is None
    m_status.assert_not_called()  # never monitor a failed submit
    m_agg.assert_not_called()


def test_verify_submitted_failed_stops_before_monitor(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    with (
        mock.patch(f"{_SEAM}.submit_pipeline", return_value=_sp_result("verify_submitted_failed")),
        mock.patch(f"{_SEAM}.status_pipeline") as m_status,
        mock.patch(f"{_SEAM}.aggregate_flow") as m_agg,
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "submit_failed"
    assert res.needs_decision is True
    m_status.assert_not_called()
    m_agg.assert_not_called()


# ── deduped proceeds to monitor ──────────────────────────────────────────────


def test_deduped_submit_still_monitors_and_aggregates(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    with (
        mock.patch(
            f"{_SEAM}.submit_pipeline",
            return_value=_sp_result("deduped", needs_decision=False),
        ),
        mock.patch(f"{_SEAM}.status_pipeline", return_value=_st_result("complete")) as m_status,
        mock.patch(f"{_SEAM}.aggregate_flow", return_value=_agg_result()) as m_agg,
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "complete"
    assert res.needs_decision is False
    m_status.assert_called_once()  # dedup = run already live → monitor it
    m_agg.assert_called_once()


def test_run_id_threaded_from_submit_into_monitor_and_aggregate(tmp_path: Path) -> None:
    """campaign-run must monitor + aggregate the run it ACTUALLY submitted
    (``sp.run_id``), not whatever run_id the caller pre-filled in the status /
    aggregate sub-specs — otherwise a misaligned spec silently watches/reduces
    the WRONG run."""
    from hpc_agent.ops.campaign_run import campaign_run

    spec = _campaign_spec()  # status + aggregate sub-specs carry "ml-abcd1234"
    submitted = _sp_result("complete", run_id="ml-deadbeef")  # but THIS is the real run

    with (
        mock.patch(f"{_SEAM}.submit_pipeline", return_value=submitted),
        mock.patch(f"{_SEAM}.status_pipeline", return_value=_st_result("complete")) as m_status,
        mock.patch(f"{_SEAM}.aggregate_flow", return_value=_agg_result()) as m_agg,
    ):
        campaign_run(tmp_path, spec=spec)

    # monitor + aggregate were re-pointed at the submitted run, not the stale spec.
    assert m_status.call_args.kwargs["spec"].monitor.run_id == "ml-deadbeef"
    assert m_agg.call_args.kwargs["spec"].run_id == "ml-deadbeef"


# ── run_failed / run_abandoned / timeout ─────────────────────────────────────


def test_run_failed_stops_before_aggregate(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    with (
        mock.patch(f"{_SEAM}.submit_pipeline", return_value=_sp_result("complete")),
        mock.patch(f"{_SEAM}.status_pipeline", return_value=_st_result("failed", failed_waves=[2])),
        mock.patch(f"{_SEAM}.aggregate_flow") as m_agg,
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "run_failed"
    assert res.needs_decision is True
    assert res.lifecycle_state == "failed"
    assert "resubmit-failed" in res.reason
    m_agg.assert_not_called()  # never aggregate a failed run


def test_run_abandoned_stops_before_aggregate(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    with (
        mock.patch(f"{_SEAM}.submit_pipeline", return_value=_sp_result("complete")),
        mock.patch(f"{_SEAM}.status_pipeline", return_value=_st_result("abandoned")),
        mock.patch(f"{_SEAM}.aggregate_flow") as m_agg,
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "run_abandoned"
    assert res.needs_decision is True
    assert res.lifecycle_state == "abandoned"
    # Guidance speaks the CLI verb (`reconcile`), never the registry name
    # `reconcile-journal` (run-#12 finding 22).
    assert "reconcile " in res.reason and "reconcile-journal" not in res.reason
    m_agg.assert_not_called()


def test_timeout_stops_with_reinvoke_reason(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    with (
        mock.patch(f"{_SEAM}.submit_pipeline", return_value=_sp_result("complete")),
        mock.patch(f"{_SEAM}.status_pipeline", return_value=_st_result("timeout")),
        mock.patch(f"{_SEAM}.aggregate_flow") as m_agg,
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "run_timeout"  # its own budget-only stage, not run_failed
    assert res.needs_decision is True
    assert res.lifecycle_state == "timeout"
    assert "Re-invoke" in res.reason and "budget" in res.reason
    m_agg.assert_not_called()  # cannot aggregate an incomplete run


# ── aggregate_failed ─────────────────────────────────────────────────────────


def test_aggregate_raise_is_aggregate_failed(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    with (
        mock.patch(f"{_SEAM}.submit_pipeline", return_value=_sp_result("complete")),
        mock.patch(f"{_SEAM}.status_pipeline", return_value=_st_result("complete")),
        mock.patch(
            f"{_SEAM}.aggregate_flow",
            side_effect=errors.CombinerFailed("wave 0 combine exhausted retries"),
        ),
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "aggregate_failed"
    assert res.needs_decision is True
    assert res.lifecycle_state == "complete"
    assert "CombinerFailed" in res.reason
    assert res.aggregate_result is None  # nothing returned to carry


def test_aggregate_partial_is_aggregate_failed(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    partial = _agg_result(
        failed_waves=[3],
        escalation_reason="combiner_failed_max_retries:waves=3",
    )
    with (
        mock.patch(f"{_SEAM}.submit_pipeline", return_value=_sp_result("complete")),
        mock.patch(f"{_SEAM}.status_pipeline", return_value=_st_result("complete")),
        mock.patch(f"{_SEAM}.aggregate_flow", return_value=partial),
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "aggregate_failed"
    assert res.needs_decision is True
    assert res.aggregate_result is not None
    assert res.aggregate_result["failed_waves"] == [3]


# ── complete ─────────────────────────────────────────────────────────────────


def test_complete_carries_aggregate_summary(tmp_path: Path) -> None:
    from hpc_agent.ops.campaign_run import campaign_run

    with (
        mock.patch(f"{_SEAM}.submit_pipeline", return_value=_sp_result("complete")),
        mock.patch(f"{_SEAM}.status_pipeline", return_value=_st_result("complete")),
        mock.patch(f"{_SEAM}.aggregate_flow", return_value=_agg_result()),
    ):
        res = campaign_run(tmp_path, spec=_campaign_spec())

    assert res.stage_reached == "complete"
    assert res.needs_decision is False
    assert res.campaign_id == "camp-iter-7"
    assert res.run_id == "ml-abcd1234"
    assert res.job_ids == ["123"]
    assert res.lifecycle_state == "complete"
    assert res.aggregate_result is not None
    assert res.aggregate_result["aggregated_metrics"]["ml-abcd1234"]["qlike"] == 0.42
