"""Detach-by-contract seam on ``campaign-run`` (design §3; run-#10 F-K).

A campaign iteration's spine (submit → monitor to terminal → aggregate) is
minutes-to-hours of cluster-bound wall-clock, so ``detach`` defaults ON. The
detach seat wraps OUTSIDE ``_campaign_run_impl`` AND the relay-due seam, so:

* the PARENT (detach on) returns a handle immediately, running neither the impl
  nor arming a relay-due marker (nothing terminal yet);
* the detached CHILD (detach off) runs the impl, arms the relay-due marker on its
  terminal, and records that terminal for the parent's idempotent replay.

Cluster-free: the launcher and ``_campaign_run_impl`` are patched at the module
boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import hpc_agent.ops.campaign_run as cr
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent._wire.workflows.campaign_run import CampaignRunResult, CampaignRunSpec
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.status_pipeline import StatusPipelineSpec
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent._wire.workflows.submit_pipeline import SubmitPipelineSpec
from hpc_agent.state.notebook_audit import read_undischarged_relay_markers

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "ml-abcd1234"
_CID = "camp-iter-7"
_LAUNCH_PATH = "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"


class _FakeLaunch:
    run_id = _RUN_ID
    pid = 4242
    log_path = "/x/detached.log"


def _campaign_spec(*, detach: bool = True) -> CampaignRunSpec:
    return CampaignRunSpec(
        submit=SubmitPipelineSpec(
            submit=SubmitAndVerifySpec(
                submit=SubmitFlowSpec(
                    profile="ml",
                    cluster="hoffman2",
                    ssh_target="u@h",
                    remote_path="/r",
                    job_name="ml",
                    run_id=_RUN_ID,
                    total_tasks=4,
                    backend="sge",
                    script=".hpc/templates/cpu_array.sh",
                    job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
                ),
            ),
        ),
        status=StatusPipelineSpec(monitor=MonitorFlowSpec(run_id=_RUN_ID)),
        aggregate=AggregateFlowSpec(run_id=_RUN_ID),
        campaign_id=_CID,
        detach=detach,
    )


def _complete_result() -> CampaignRunResult:
    return CampaignRunResult(
        stage_reached="complete",
        needs_decision=False,
        reason="iteration complete",
        campaign_id=_CID,
        run_id=_RUN_ID,
        job_ids=["123"],
        lifecycle_state="complete",
        aggregate_result={"aggregated_metrics": {}},
    )


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


def test_campaign_detaches_by_default(tmp_path: Path) -> None:
    """detach ON (default): the iteration never runs in-process — a durable worker
    is spawned and the handle envelope returned. The child spec carries detach OFF.
    The PARENT arms NO relay-due marker (nothing terminal yet)."""
    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(cr, "_campaign_run_impl") as m_impl,
    ):
        result = cr.campaign_run(tmp_path, spec=_campaign_spec())

    m_impl.assert_not_called()
    m_launch.assert_called_once()
    assert m_launch.call_args.kwargs["verb"] == "campaign-run"
    assert m_launch.call_args.kwargs["spec"]["detach"] is False
    assert result.stage_reached == "detached"
    assert result.started is True
    assert result.watch == "journal"
    assert result.detached_pid == 4242
    assert result.run_id == _RUN_ID
    # The parent detach never arms relay-due — only the child's real terminal does.
    assert read_undischarged_relay_markers(tmp_path, _CID, scope_kind="campaign") == []


def test_sync_child_arms_relay_due_and_records_terminal(tmp_path: Path) -> None:
    """The detached CHILD (detach off) runs the impl, arms the relay-due marker on
    its terminal, and records that terminal for replay — the 522ecb39 seam, intact
    UNDER detach-by-contract."""
    _sidecar(tmp_path)
    with mock.patch.object(cr, "_campaign_run_impl", return_value=_complete_result()) as m_impl:
        result = cr.campaign_run(tmp_path, spec=_campaign_spec(detach=False))

    m_impl.assert_called_once()
    assert result.stage_reached == "complete"
    # relay-due marker armed on the campaign scope (the omission gate's 2nd source).
    markers = read_undischarged_relay_markers(tmp_path, _CID, scope_kind="campaign")
    assert len(markers) == 1
    assert markers[0]["record_kind"] == "campaign-run"
    assert "complete" in markers[0]["key_tokens"]


def test_campaign_replays_recorded_terminal_without_respawn(tmp_path: Path) -> None:
    """After the child recorded its terminal, a detach=on re-invoke REPLAYS it
    instead of re-submitting a fresh array — the launcher is never called."""
    _sidecar(tmp_path)

    # 1. The child runs synchronously and records its terminal.
    with mock.patch.object(cr, "_campaign_run_impl", return_value=_complete_result()):
        sync = cr.campaign_run(tmp_path, spec=_campaign_spec(detach=False))
    assert sync.stage_reached == "complete"

    # 2. The parent's re-invoke (detach on) replays it — no spawn, no re-run.
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        mock.patch.object(cr, "_campaign_run_impl") as m_impl,
    ):
        replay = cr.campaign_run(tmp_path, spec=_campaign_spec())

    m_launch.assert_not_called()
    m_impl.assert_not_called()
    assert replay.stage_reached == "complete"
    assert replay.run_id == _RUN_ID


def test_detached_child_spec_digs_run_id_from_aggregate(tmp_path: Path) -> None:
    """The campaign spec carries no ``submit.submit.run_id``; the launcher must dig
    the poll key from ``aggregate.run_id`` (via _block_spec_run_id)."""
    from hpc_agent._kernel.lifecycle import detached

    captured: dict[str, Any] = {}

    def _capture(*, run_id: str, block: str, argv: list[str], log_path: Any, cwd: str):  # noqa: ANN401
        captured["run_id"] = run_id
        captured["block"] = block
        return _FakeLaunch()

    with mock.patch.object(detached, "_spawn_detached", _capture):
        detached.launch_submit_block_detached(
            verb="campaign-run",
            experiment_dir=str(tmp_path),
            spec=_campaign_spec(detach=False).model_dump(mode="json"),
        )

    assert captured["run_id"] == _RUN_ID
    assert captured["block"] == "campaign-run"
