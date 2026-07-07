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
from hpc_agent.ops.relay_render import render_relay

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


def test_s1_clean_walk_without_resolve_reason_flags_pre_resolve_boundary(tmp_path: Path) -> None:
    """Run #7 block-drive seam: a clean walk with no resolve spec is the
    PRE-RESOLVE boundary — run_id is unminted (resolve needs caller inputs the
    walk cannot supply, e.g. remote_path). ``next_block`` STAYS submit-s2 (the
    code-driven ``("submit-s1","resolved")->submit-s2`` table target — special-
    casing it to None breaks the block↔SUCCESSORS agreement contract), but the
    brief's REASON must flag that run_id is unminted and direct the caller to
    supply resolve FIRST — so the agent doesn't read the submit-s2 pointer as
    "advance now" and jump ahead of the resolve leg (which sent the demo agent
    off-driver into a hand-called submit-s2). The routing fix is the reason +
    the hpc-submit skill's pre-resolve step, not a table change.
    """
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
    result = blocks.submit_s1(tmp_path, spec=SubmitS1Spec(walk=walk, run_preflight=False))

    assert result.stage_reached == "resolved"
    assert result.needs_decision is True
    assert result.run_id is None  # unminted at the pre-resolve boundary
    # next_block stays the table target; the guidance lives in the reason.
    assert result.next_block is not None and result.next_block["verb"] == "submit-s2"
    assert "resolve" in result.reason.lower()  # directs supplying the resolve inputs
    assert "run_id" in result.reason  # names WHY it is not yet submittable (unminted)


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


def _monitor_result_flat(*, lifecycle_state: str, last_status: dict):
    """A MonitorFlowResult whose ``last_status`` carries the counts FLAT.

    This is the real ``MonitorFlowResult.last_status`` shape (counts keyed
    directly, alongside ``checked_at``) — the S3 arm path must project it, not
    reach for a nonexistent ``["summary"]`` nesting (run #8 regression).
    """
    from hpc_agent.ops.monitor_flow import MonitorFlowResult

    return MonitorFlowResult(
        run_id=_RUN_ID,
        lifecycle_state=lifecycle_state,
        last_status=last_status,
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


def test_s3_terminal_flat_last_status_arms_none(tmp_path: Path) -> None:
    """Run #8 regression: a 20/20-complete FLAT ``last_status`` must arm "none".

    The live bug reached for ``last_status["summary"]`` (which does not exist on
    the flat shape), fed ``summary={}`` to the REAL ``decide_monitor_arm``, and
    fell through to a ``*/1`` running-fallback cron on an already-terminal run.
    ``decide_monitor_arm`` is NOT mocked here so the projection is exercised
    end-to-end.
    """
    _greenlight(tmp_path, "submit-s3")
    flat = {"checked_at": "2026-07-06T03:00:00Z", "complete": 10, "running": 0, "pending": 0}
    with (
        mock.patch.object(
            blocks,
            "launch_main_array",
            return_value=_sv_result(verified=True, job_ids=["999"]),
        ),
        mock.patch.object(
            blocks,
            "monitor_flow",
            return_value=_monitor_result_flat(lifecycle_state="complete", last_status=flat),
        ),
    ):
        result = blocks.submit_s3(tmp_path, spec=_s3_spec())

    assert result.brief["monitor_arm"]["arm"] == "none"
    assert result.brief["monitor_arm"]["reason"] == "complete"
    assert result.brief["monitor_arm"]["schedule"] is None


def test_s3_running_flat_last_status_arms_cron(tmp_path: Path) -> None:
    """Legit path: a still-running FLAT ``last_status`` arms an adaptive cron."""
    _greenlight(tmp_path, "submit-s3")
    flat = {"checked_at": "2026-07-06T03:00:00Z", "complete": 0, "running": 10, "pending": 0}
    with (
        mock.patch.object(
            blocks,
            "launch_main_array",
            return_value=_sv_result(verified=True, job_ids=["999"]),
        ),
        mock.patch.object(
            blocks,
            "monitor_flow",
            return_value=_monitor_result_flat(lifecycle_state="timeout", last_status=flat),
        ),
    ):
        result = blocks.submit_s3(tmp_path, spec=_s3_spec())

    assert result.brief["monitor_arm"]["arm"] == "cron"
    assert result.brief["monitor_arm"]["reason"] == "running_fallback"
    assert result.brief["monitor_arm"]["schedule"] is not None


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
    spec = SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
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
    spec = SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
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

    spec = SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
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


# ── idempotent terminal replay (run #7: re-invoke ≠ re-spawn) ────────────────


def _sidecar(tmp_path: Path, *, cmd_sha: str, run_id: str = _RUN_ID) -> None:
    """Write a per-run sidecar so ``_current_cmd_sha`` has a tree fingerprint to
    key the terminal replay on (the replay refuses on an absent/empty sha)."""
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=10,
        tasks_py_sha="",
    )


def test_s2_reinvoke_replays_recorded_terminal_without_respawn(tmp_path: Path) -> None:
    """Run #7 papercut root: once a detached S2 worker has driven the block to its
    terminal for THIS tree, a re-invoke replays that recorded outcome instead of
    spawning a fresh worker (no SSH, no new canary)."""
    _sidecar(tmp_path, cmd_sha="sha-A")
    _greenlight(tmp_path, "submit-s2")

    # First completion (the detached worker's synchronous body) records the terminal.
    with mock.patch.object(
        blocks, "submit_and_verify", return_value=_sv_result(verified=True, job_ids=[])
    ):
        first = blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec(), detach=False))
    assert first.stage_reached == "canary_verified"

    # Re-invoke in DETACH mode: must replay, never spawn.
    with mock.patch(
        "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"
    ) as m_launch:
        replayed = blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec(), detach=True))

    m_launch.assert_not_called()
    assert replayed.stage_reached == "canary_verified"
    assert replayed.needs_decision is True
    assert replayed.next_block is not None  # the S3 pointer survives the replay
    assert replayed.brief["est_core_hours"] == first.brief["est_core_hours"]


def test_s2_reinvoke_after_nudge_respawns(tmp_path: Path) -> None:
    """A nudge moves the tree (revise-resolved rewrites the sidecar cmd_sha); the
    recorded terminal is then STALE, so the re-invoke must re-execute (spawn),
    never replay a canary that verified the old tree."""
    _sidecar(tmp_path, cmd_sha="sha-A")
    _greenlight(tmp_path, "submit-s2")
    with mock.patch.object(
        blocks, "submit_and_verify", return_value=_sv_result(verified=True, job_ids=[])
    ):
        blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec(), detach=False))

    _sidecar(tmp_path, cmd_sha="sha-B")  # the nudge

    from hpc_agent._kernel.lifecycle.detached import DetachedLaunch

    fake = DetachedLaunch(run_id=_RUN_ID, pid=4321, log_path="x.log", argv=["x"])
    with mock.patch(
        "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached", return_value=fake
    ) as m_launch:
        out = blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec(), detach=True))

    m_launch.assert_called_once()  # stale record → re-spawn, not replay
    assert out.stage_reached == "detached"


def test_s3_clean_terminal_is_replayable(tmp_path: Path) -> None:
    """The S3-clean-terminal sibling: a clean completion is ``needs_decision=False``
    so the provenance-brief journal never stored it (agent scraped the worker
    log). The terminal store DOES record it — a re-invoke replays the clean result
    with its ``submit-s4`` pointer, no re-launch of the main array + watch."""
    _sidecar(tmp_path, cmd_sha="sha-A")
    _greenlight(tmp_path, "submit-s3")
    with (
        mock.patch.object(
            blocks, "launch_main_array", return_value=_sv_result(verified=True, job_ids=["999"])
        ),
        mock.patch.object(
            blocks, "monitor_flow", return_value=_monitor_result(lifecycle_state="complete")
        ),
        mock.patch.object(
            blocks, "decide_monitor_arm", return_value={"arm": "none", "cadence_sec": 0}
        ),
    ):
        first = blocks.submit_s3(tmp_path, spec=_s3_spec())
    assert first.stage_reached == "watching_terminal"
    assert first.needs_decision is False

    s3_detach = SubmitS3Spec(
        submit=_sv_spec(),
        canary_run_id=f"{_RUN_ID}_canary",
        canary_job_ids=["12344"],
        monitor=MonitorFlowSpec(run_id=_RUN_ID),
        invocation_argv="monitor-hpc --run-id " + _RUN_ID,
        detach=True,
    )
    with mock.patch(
        "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"
    ) as m_launch:
        replayed = blocks.submit_s3(tmp_path, spec=s3_detach)

    m_launch.assert_not_called()
    assert replayed.stage_reached == "watching_terminal"
    assert replayed.needs_decision is False
    assert replayed.next_block is not None  # the S4 pointer the agent needs


def test_s4_reinvoke_replays_recorded_terminal_without_respawn(tmp_path: Path) -> None:
    """S4 joins the detached blocks (design §3): once a worker has driven the
    harvest to its terminal for THIS tree, a re-invoke replays the recorded
    results brief instead of spawning a fresh worker (no SSH, no re-combine)."""
    _sidecar(tmp_path, cmd_sha="sha-A")
    _greenlight(tmp_path, "submit-s4")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()):
        first = blocks.submit_s4(
            tmp_path, spec=SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
        )
    assert first.stage_reached == "harvested"

    with mock.patch(
        "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"
    ) as m_launch:
        replayed = blocks.submit_s4(
            tmp_path, spec=SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=True)
        )

    m_launch.assert_not_called()
    assert replayed.stage_reached == "harvested"
    assert replayed.needs_decision is True
    assert replayed.brief["results_table"] == first.brief["results_table"]


def test_replay_does_not_double_append_provenance_brief(tmp_path: Path) -> None:
    """A replay returns the SAME terminal — it must not append a second identical
    brief to the append-only provenance journal (two identical s2 briefs were
    seen live before this fix)."""
    from hpc_agent.state.decision_briefs import read_briefs

    _sidecar(tmp_path, cmd_sha="sha-A")
    _greenlight(tmp_path, "submit-s2")
    with mock.patch.object(
        blocks, "submit_and_verify", return_value=_sv_result(verified=True, job_ids=[])
    ):
        blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec(), detach=False))
    with mock.patch("hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"):
        blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec(), detach=True))

    s2_briefs = [r for r in read_briefs(tmp_path, _RUN_ID) if r["block"] == "s2"]
    assert len(s2_briefs) == 1


# ── relay rendering (finding 15: code authors the relay; agent relays verbatim) ──


def test_render_relay_s2_canary_summary_uses_canary_one_task_not_main_total() -> None:
    """The exact finding-15 bleed: the S2 canary relay renders the CANARY's 1
    task, NEVER the main array's total_tasks (which rides ``cost_estimate``)."""
    brief = {
        "run_id": "ml_run_abcd1234",
        "cluster": "hoffman2",
        "canary_run_id": "ml_run_abcd1234_canary",
        "verified": True,
        "failure_kind": None,
        "deduped": False,
        "est_core_hours": 80.0,
        "cost_estimate": {"total_tasks": 20, "walltime_s": 3600, "cores_per_task": 4},
    }
    relay = render_relay("s2", "canary_verified", brief)

    assert "canary green" in relay
    assert "1 task" in relay  # the canary is a one-task probe, by construction
    assert "80" in relay  # est core-hours — legitimately shown
    assert "hoffman2" in relay
    # The main array's total (20) must NOT bleed into the 1-task canary summary.
    assert "20" not in relay


def test_render_relay_s2_canary_failed_names_the_failure() -> None:
    brief = {
        "run_id": "ml_run_abcd1234",
        "cluster": "hoffman2",
        "canary_run_id": "ml_run_abcd1234_canary",
        "verified": False,
        "failure_kind": "import_error",
    }
    relay = render_relay("s2", "canary_failed", brief)
    assert "canary failed verification (import_error)" in relay
    assert "propose a fix" in relay


def test_render_relay_s2_canary_never_queued_is_distinct() -> None:
    """A canary that never entered the queue (canary_run_id None) is rendered
    distinctly from a genuine verification failure."""
    brief = {"run_id": "ml_run_abcd1234", "cluster": "hoffman2", "canary_run_id": None}
    relay = render_relay("s2", "canary_failed", brief)
    assert "never entered the queue" in relay
    assert "failed verification" not in relay


def test_render_relay_s3_terminal_counts_from_summary() -> None:
    brief = {
        "main_run_id": "ml_run_abcd1234",
        "cluster": "hoffman2",
        "total_tasks": 10,
        "last_status": {"summary": {"complete": 10, "running": 0, "pending": 0, "failed": 0}},
    }
    relay = render_relay("s3", "watching_terminal", brief)
    assert "main array complete" in relay
    assert "10/10 tasks" in relay
    assert "ml_run_abcd1234" in relay


def test_render_relay_s4_harvest_counts_rows() -> None:
    brief = {"run_id": "ml_run_abcd1234", "results_table": [{"key": "a"}, {"key": "b"}]}
    relay = render_relay("s4", "harvested", brief)
    assert "harvest complete" in relay
    assert "2 result row(s)" in relay


def test_render_relay_s1_resolved_greenlights_to_canary() -> None:
    brief = {"resolved": {"cluster": "hoffman2"}, "provenance": {}, "ambiguities": []}
    relay = render_relay("s1", "resolved", brief)
    assert "resolved" in relay
    assert "hoffman2" in relay
    assert "stage & canary" in relay


def test_s2_result_carries_code_rendered_relay(tmp_path: Path) -> None:
    """The S2 result carries a code-rendered relay the agent forwards verbatim —
    canary green + the canary's 1 task + the est core-hours."""
    spec = SubmitS2Spec(submit=_sv_spec(), detach=False)
    _greenlight(tmp_path, "submit-s2")

    with mock.patch.object(
        blocks, "submit_and_verify", return_value=_sv_result(verified=True, job_ids=[])
    ):
        result = blocks.submit_s2(tmp_path, spec=spec)

    assert "canary green" in result.relay
    assert "1 task" in result.relay
    assert f"{result.brief['est_core_hours']:g}" in result.relay
    # The relay is NOT persisted into the brief: a stale relay string in the
    # durable record would poison the verify-relay source pool (the audit must
    # diff the agent's relay against the STRUCTURED facts, not a prior rendering).
    assert "relay" not in result.brief


def test_s3_terminal_result_carries_relay(tmp_path: Path) -> None:
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
    ):
        result = blocks.submit_s3(tmp_path, spec=_s3_spec())

    assert "main array complete" in result.relay
    assert "10/10 tasks" in result.relay


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
