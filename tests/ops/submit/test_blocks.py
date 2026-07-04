"""Tests for the submit S1–S4 human-amplification block verbs + the
``submit_and_verify`` canary/main split (docs/design/human-amplification-blocks.md §3).

Cluster-free: the composed rings (submit-preflight / submit-and-verify /
launch-main-array / monitor-flow / decide-monitor-arm / aggregate-flow) are
mocked at the ``blocks`` module boundary, so these assert the block orchestration
+ brief digestion, never SSH or a scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import hpc_agent.ops.submit_blocks as blocks
from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.submit_and_verify import (
    SubmitAndVerifyResult,
    SubmitAndVerifySpec,
)
from hpc_agent._wire.workflows.submit_blocks import (
    SubmitS1Spec,
    SubmitS2Spec,
    SubmitS3Spec,
    SubmitS4Spec,
)
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "ml_run_abcd1234"


def _greenlight(experiment_dir: Path, verb: str, *, run_id: str = _RUN_ID) -> None:
    """Journal a human ``y`` greenlight naming *verb* (the gate's precondition).

    The sequenced block verbs (submit-s2/s3/s4, aggregate-run) refuse to act
    unless the latest run-scoped decision is a ``y`` whose ``resolved.next_block``
    names them (docs/design/human-amplification-blocks.md §2). Tests that drive a
    gated block past its precondition record that greenlight first.
    """
    from hpc_agent.state.decision_journal import append_decision

    append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=run_id,
        block="test-greenlight",
        response="y",
        resolved={"next_block": verb},
    )


# ── shared fixtures ──────────────────────────────────────────────────────────


def _submit_flow_spec(
    *, canary: bool = True, walltime_sec: int = 3600, cpus: int = 4
) -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=_RUN_ID,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=canary,
        resources=SubmitResources(walltime_sec=walltime_sec, cpus=cpus),
    )


def _sv_spec() -> SubmitAndVerifySpec:
    return SubmitAndVerifySpec(submit=_submit_flow_spec(), poll_interval_sec=1, wait_budget_sec=5)


def _sv_result(
    *,
    verified: bool,
    job_ids: list[str],
    deduped: bool = False,
    failure_kind: Any = None,
    canary_run_id: Any = f"{_RUN_ID}_canary",
) -> SubmitAndVerifyResult:
    return SubmitAndVerifyResult(
        run_id=_RUN_ID,
        job_ids=job_ids,
        total_tasks=10,
        deduped=deduped,
        canary_run_id=canary_run_id,
        canary_job_ids=["12344"],
        verified=verified,
        failure_kind=failure_kind,
        verify_result=None,
    )


# ── S1: recommendations, not auto-application ────────────────────────────────


def test_s1_surfaces_safe_default_as_recommendation_not_applied(tmp_path: Path) -> None:
    """The load-bearing S1 invariant (§6): every ambiguity's safe_default is
    surfaced as a `recommendation` in the brief and is NOT written into
    `resolved` (apply-safe-defaults is the silent actor this kills)."""
    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": None,
            "configured_clusters": ["carc", "hoffman2"],
            "goal": "sweep ridge",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    spec = SubmitS1Spec(walk=walk, run_preflight=False)

    result = blocks.submit_s1(tmp_path, spec=spec)

    assert result.block == "s1"
    assert result.stage_reached == "needs_resolution"
    assert result.needs_decision is True
    # cluster is ambiguous with a safe_default → surfaced as a recommendation.
    cluster_amb = next(a for a in result.brief["ambiguities"] if a["field"] == "cluster")
    assert cluster_amb["recommendation"] == "carc"  # first lexicographically
    assert cluster_amb["safe_default"] == "carc"
    # NOT auto-applied: `resolved` must not carry a picked cluster.
    assert "cluster" not in result.brief["resolved"]


def test_s1_runs_preflight_and_folds_into_brief(tmp_path: Path) -> None:
    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": "hoffman2",
            "goal": "g",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    spec = SubmitS1Spec(walk=walk, run_preflight=True)

    with mock.patch.object(blocks, "submit_preflight", return_value={"overall": "pass"}) as m_pf:
        result = blocks.submit_s1(tmp_path, spec=spec)

    m_pf.assert_called_once()
    assert result.brief["preflight"] == {"overall": "pass"}
    # No ambiguities, no resolve spec → clean resolved terminator, still y/nudge.
    assert result.stage_reached == "resolved"
    assert result.needs_decision is True


# ── submit_and_verify split (backward-compat) ────────────────────────────────


def _live_submit_result(*, canary: bool, job_ids: list[str]):
    from hpc_agent.ops.submit_flow import SubmitFlowResult

    return SubmitFlowResult(
        run_id=_RUN_ID,
        job_ids=job_ids,
        total_tasks=10,
        deduped=False,
        canary_done=canary,
        canary_run_id=f"{_RUN_ID}_canary" if canary else None,
        canary_job_ids=["12344"] if canary else None,
    )


def _verify_ok() -> dict[str, Any]:
    return {
        "ok": True,
        "failure_kind": None,
        "details": "ok",
        "stderr_tail": "",
        "metrics_fingerprint": None,
    }


def test_stop_after_canary_does_not_launch_main() -> None:
    """submit_and_verify(stop_after_canary=True): Phase 1 (canary) runs, canary
    verifies, and the main array is NOT launched — one submit_flow call, empty
    job_ids, verified=True."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_live_submit_result(canary=True, job_ids=["999"]),
        ) as m_submit,
        mock.patch("hpc_agent.ops.submit_and_verify.verify_canary", return_value=_verify_ok()),
    ):
        result = submit_and_verify(None, spec=_sv_spec(), stop_after_canary=True)  # type: ignore[arg-type]

    assert m_submit.call_count == 1  # only Phase 1 (canary_only); main never launched
    assert result.verified is True
    assert result.job_ids == []
    assert result.canary_run_id == f"{_RUN_ID}_canary"


def test_default_still_launches_main_backward_compat() -> None:
    """Default (stop_after_canary=False): the fused #160 behavior is preserved —
    a verified canary flows straight into the main-array launch (two submit_flow
    calls, populated job_ids)."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_live_submit_result(canary=True, job_ids=["999"]),
        ) as m_submit,
        mock.patch("hpc_agent.ops.submit_and_verify.verify_canary", return_value=_verify_ok()),
    ):
        result = submit_and_verify(None, spec=_sv_spec())  # type: ignore[arg-type]

    assert m_submit.call_count == 2  # Phase 1 canary + Phase 2 main
    assert result.verified is True
    assert result.job_ids == ["999"]


def test_launch_main_array_issues_phase2_flips(tmp_path: Path) -> None:
    """launch_main_array (the S3 seam) launches the main array with the Phase-2
    deterministic flips: canary off + the internal skip kwargs."""
    from hpc_agent.ops.submit_and_verify import launch_main_array

    # No sidecar under tmp_path → the post-greenlight drift guard cannot prove
    # drift (absent baseline) and launches, exercising the Phase-2 flips below.
    with mock.patch(
        "hpc_agent.ops.submit_and_verify.submit_flow",
        return_value=_live_submit_result(canary=False, job_ids=["777"]),
    ) as m_submit:
        result = launch_main_array(
            tmp_path,
            spec=_sv_spec(),
            canary_run_id=f"{_RUN_ID}_canary",
            canary_job_ids=["12344"],
        )

    m_submit.assert_called_once()
    call = m_submit.call_args
    assert call.kwargs["spec"].canary is False
    assert call.kwargs["_skip_preflight"] is True
    assert call.kwargs["_skip_rsync_deploy"] is True
    assert result.verified is True
    assert result.job_ids == ["777"]
    assert result.canary_run_id == f"{_RUN_ID}_canary"


# ── S2: stops after canary, attaches est_core_hours ──────────────────────────


def test_s2_stops_after_canary_and_attaches_est_core_hours(tmp_path: Path) -> None:
    spec = SubmitS2Spec(submit=_sv_spec(), detach=False)
    _greenlight(tmp_path, "submit-s2")

    with mock.patch.object(
        blocks,
        "submit_and_verify",
        return_value=_sv_result(verified=True, job_ids=[]),
    ) as m_sv:
        result = blocks.submit_s2(tmp_path, spec=spec)

    # Composed submit-and-verify was invoked with stop_after_canary=True.
    assert m_sv.call_args.kwargs["stop_after_canary"] is True
    assert result.block == "s2"
    assert result.stage_reached == "canary_verified"
    assert result.needs_decision is True
    # est_core_hours = 10 tasks × 3600s × 4 cores / 3600 = 40.0 core-hours.
    assert result.brief["est_core_hours"] == 40.0
    assert result.brief["cost_estimate"]["cores_per_task"] == 4


def test_s2_surfaces_canary_failure(tmp_path: Path) -> None:
    spec = SubmitS2Spec(submit=_sv_spec(), detach=False)
    _greenlight(tmp_path, "submit-s2")

    with mock.patch.object(
        blocks,
        "submit_and_verify",
        return_value=_sv_result(verified=False, job_ids=[], failure_kind="import_error"),
    ):
        result = blocks.submit_s2(tmp_path, spec=spec)

    assert result.stage_reached == "canary_failed"
    assert result.needs_decision is True
    assert result.brief["failure_kind"] == "import_error"
    # A canary that LANDED and failed verification names the failure_kind.
    assert "failed verification" in result.reason
    # The estimate is still attached (the human sizes the fix against the footprint).
    assert result.brief["est_core_hours"] == 40.0


def test_s2_distinguishes_canary_never_landed_from_verification_failure(tmp_path: Path) -> None:
    """A canary that never entered the queue (verified=False, canary_run_id=None,
    failure_kind=None) must NOT be rendered as a "failed verification (None)" — it
    gets a distinct "never entered the queue" reason (still a canary_failed
    anomaly terminator → human decides)."""
    spec = SubmitS2Spec(submit=_sv_spec(), detach=False)
    _greenlight(tmp_path, "submit-s2")

    with mock.patch.object(
        blocks,
        "submit_and_verify",
        return_value=_sv_result(verified=False, job_ids=[], failure_kind=None, canary_run_id=None),
    ):
        result = blocks.submit_s2(tmp_path, spec=spec)

    assert result.stage_reached == "canary_failed"
    assert result.needs_decision is True
    assert "never entered the queue" in result.reason
    # Distinct from the genuine-verification-failure wording.
    assert "failed verification" not in result.reason


# ── S3: launches main + arms monitor ─────────────────────────────────────────


def _monitor_result(*, lifecycle_state: str):
    from hpc_agent.ops.monitor_flow import MonitorFlowResult

    return MonitorFlowResult(
        run_id=_RUN_ID,
        lifecycle_state=lifecycle_state,
        last_status={"summary": {"complete": 10, "running": 0, "pending": 0, "failed": 0}},
        combined_waves=[],
        failed_waves=[],
        ticks=3,
        elapsed_seconds=42.0,
        escalation_reason=None,
    )


def _s3_spec() -> SubmitS3Spec:
    return SubmitS3Spec(
        submit=_sv_spec(),
        canary_run_id=f"{_RUN_ID}_canary",
        canary_job_ids=["12344"],
        monitor=MonitorFlowSpec(run_id=_RUN_ID),
        invocation_argv="monitor-hpc --run-id " + _RUN_ID,
        detach=False,
    )


def test_s3_launches_main_and_arms_monitor(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s3")
    with (
        mock.patch.object(
            blocks,
            "launch_main_array",
            return_value=_sv_result(verified=True, job_ids=["999"]),
        ) as m_launch,
        mock.patch.object(
            blocks,
            "monitor_flow",
            return_value=_monitor_result(lifecycle_state="complete"),
        ) as m_mon,
        mock.patch.object(
            blocks,
            "decide_monitor_arm",
            return_value={"arm": "none", "cadence_sec": 0},
        ) as m_arm,
    ):
        result = blocks.submit_s3(tmp_path, spec=_s3_spec())

    m_launch.assert_called_once()
    m_mon.assert_called_once()
    m_arm.assert_called_once()
    # The arm was driven by the monitor's final summary + the main run's tasks.
    arm_spec = m_arm.call_args.kwargs["spec"]
    assert arm_spec.summary == {"complete": 10, "running": 0, "pending": 0, "failed": 0}
    assert arm_spec.total_tasks == 10
    assert result.block == "s3"
    assert result.stage_reached == "watching_terminal"
    assert result.needs_decision is False  # clean terminal → proceed to S4
    assert result.brief["monitor_arm"] == {"arm": "none", "cadence_sec": 0}
    assert result.brief["main_job_ids"] == ["999"]
    # §5 watchdog status rides the brief arming the long wait (opt-in install —
    # the brief carries the recommendation; nothing is auto-installed).
    assert "installed" in result.brief["watchdog"]


def test_s3_brief_recommends_watchdog_install_when_missing(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s3")
    with (
        mock.patch.object(
            blocks,
            "launch_main_array",
            return_value=_sv_result(verified=True, job_ids=["999"]),
        ),
        mock.patch.object(
            blocks,
            "monitor_flow",
            return_value=_monitor_result(lifecycle_state="complete"),
        ),
        mock.patch.object(
            blocks,
            "decide_monitor_arm",
            return_value={"arm": "none", "cadence_sec": 0},
        ),
        mock.patch(
            "hpc_agent.ops.recover.doctor_install.watchdog_installed",
            return_value=False,
        ),
    ):
        result = blocks.submit_s3(tmp_path, spec=_s3_spec())

    watchdog = result.brief["watchdog"]
    assert watchdog["installed"] is False
    assert "doctor-install" in watchdog["recommendation"]


def test_s3_brief_watchdog_installed_carries_no_recommendation(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s3")
    with (
        mock.patch.object(
            blocks,
            "launch_main_array",
            return_value=_sv_result(verified=True, job_ids=["999"]),
        ),
        mock.patch.object(
            blocks,
            "monitor_flow",
            return_value=_monitor_result(lifecycle_state="complete"),
        ),
        mock.patch.object(
            blocks,
            "decide_monitor_arm",
            return_value={"arm": "none", "cadence_sec": 0},
        ),
        mock.patch(
            "hpc_agent.ops.recover.doctor_install.watchdog_installed",
            return_value=True,
        ),
    ):
        result = blocks.submit_s3(tmp_path, spec=_s3_spec())

    assert result.brief["watchdog"] == {"installed": True}


def test_s3_anomaly_is_a_terminator(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s3")
    with (
        mock.patch.object(
            blocks,
            "launch_main_array",
            return_value=_sv_result(verified=True, job_ids=["999"]),
        ),
        mock.patch.object(
            blocks,
            "monitor_flow",
            return_value=_monitor_result(lifecycle_state="failed"),
        ),
        mock.patch.object(blocks, "decide_monitor_arm", return_value={"arm": "none"}),
    ):
        result = blocks.submit_s3(tmp_path, spec=_s3_spec())

    assert result.stage_reached == "watching_anomaly"
    assert result.needs_decision is True


# ── S4: results brief ────────────────────────────────────────────────────────


def _agg_result(*, escalation_reason: str | None = None, failed_waves=None):
    from hpc_agent.ops.aggregate_flow import AggregateFlowResult

    return AggregateFlowResult(
        run_id=_RUN_ID,
        combined_waves=[0, 1],
        failed_waves=failed_waves or [],
        waves_combined_this_call=[0, 1],
        combiner_dir_local="/tmp/agg/_combiner",
        aggregated_metrics={"ridge_h5": {"rmse": 0.12}, "ridge_h1": {"rmse": 0.20}},
        escalation_reason=escalation_reason,
    )


def test_s4_returns_results_brief(tmp_path: Path) -> None:
    spec = SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(tmp_path, "submit-s4")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()) as m_agg:
        result = blocks.submit_s4(tmp_path, spec=spec)

    m_agg.assert_called_once()
    assert result.block == "s4"
    assert result.stage_reached == "harvested"
    assert result.needs_decision is True
    # Code-extracted results table (row per grid key, sorted).
    table = result.brief["results_table"]
    assert [r["key"] for r in table] == ["ridge_h1", "ridge_h5"]
    assert table[1]["metrics"] == {"rmse": 0.12}
    # The interpretation slot is handed over EMPTY — the human concludes (§2).
    assert result.brief["proposed_interpretations"] == []


def test_s4_partial_harvest_when_waves_escalate(tmp_path: Path) -> None:
    spec = SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(tmp_path, "submit-s4")

    with mock.patch.object(
        blocks,
        "aggregate_flow",
        return_value=_agg_result(escalation_reason="combiner_failed_max_retries:waves=3"),
    ):
        result = blocks.submit_s4(tmp_path, spec=spec)

    assert result.stage_reached == "harvest_partial"
    assert result.needs_decision is True
    assert result.brief["escalation_reason"].startswith("combiner_failed")


# ── brief persistence (conduct rule 9, provenance gate) ──────────────────────


def test_s2_persists_brief_for_provenance_gate(tmp_path: Path) -> None:
    """At its decision point S2 durably persists its brief so append-decision's
    provenance gate can diff a later S2→S3 greenlight against it."""
    from hpc_agent.state.decision_briefs import latest_brief_for_block

    spec = SubmitS2Spec(submit=_sv_spec(), detach=False)
    _greenlight(tmp_path, "submit-s2")

    with mock.patch.object(
        blocks, "submit_and_verify", return_value=_sv_result(verified=True, job_ids=[])
    ):
        result = blocks.submit_s2(tmp_path, spec=spec)

    persisted = latest_brief_for_block(tmp_path, _RUN_ID, "s2")
    assert persisted is not None
    assert persisted["block"] == "s2"
    assert persisted["brief"]["est_core_hours"] == result.brief["est_core_hours"]


def test_s4_persists_brief_for_provenance_gate(tmp_path: Path) -> None:
    from hpc_agent.state.decision_briefs import latest_brief_for_block

    spec = SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(tmp_path, "submit-s4")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()):
        blocks.submit_s4(tmp_path, spec=spec)

    persisted = latest_brief_for_block(tmp_path, _RUN_ID, "s4")
    assert persisted is not None
    assert "results_table" in persisted["brief"]


def test_s1_ambiguity_branch_does_not_persist_without_run_id(tmp_path: Path) -> None:
    """S1's pre-resolve ambiguity branch has no run_id yet — nothing to scope a
    brief file to, so it legitimately persists nothing (the gate then fails open
    for that greenlight)."""
    from hpc_agent.state.decision_briefs import read_briefs

    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": None,
            "configured_clusters": ["carc", "hoffman2"],
            "goal": "sweep ridge",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    result = blocks.submit_s1(tmp_path, spec=SubmitS1Spec(walk=walk, run_preflight=False))

    assert result.run_id is None
    assert read_briefs(tmp_path, _RUN_ID) == []


# ── registry metadata ────────────────────────────────────────────────────────


def test_blocks_are_agent_facing_workflows() -> None:
    from hpc_agent._kernel.registry.primitive import get_meta, register_primitives

    register_primitives()
    for name in ("submit-s1", "submit-s2", "submit-s3", "submit-s4"):
        meta = get_meta(name)
        assert meta.verb == "workflow"
        assert meta.agent_facing is True
        assert meta.cli is not None
        assert meta.cli.spec_arg is True
