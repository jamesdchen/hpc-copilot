"""Tests for the ``submit-pipeline`` composite.

The pipeline composes three workflow verbs (``submit-and-verify`` →
``verify-submitted`` → ``prepare-followup-specs``); these tests mock each at
the ``submit_pipeline`` module seam and exercise every ``stage_reached`` path —
no cluster, no journal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

from hpc_agent._wire.workflows.submit_and_verify import (
    SubmitAndVerifyResult,
    SubmitAndVerifySpec,
)
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent._wire.workflows.submit_pipeline import SubmitPipelineSpec

if TYPE_CHECKING:
    from pathlib import Path


def _pipeline_spec() -> SubmitPipelineSpec:
    return SubmitPipelineSpec(
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
                job_env={
                    "EXECUTOR": "python3 .hpc/_hpc_dispatch.py",
                    "HPC_CMD_SHA": "deadbeef",
                },
            ),
        ),
        profile="ml",
    )


def _sv_result(**kw: Any) -> SubmitAndVerifyResult:
    base: dict[str, Any] = {
        "run_id": "ml-abcd1234",
        "job_ids": ["123"],
        "total_tasks": 4,
        "deduped": False,
        "verified": True,
    }
    base.update(kw)
    return SubmitAndVerifyResult(**base)


def _followup() -> dict[str, Any]:
    return {
        "monitor_spec_path": "/r/monitor_spec.json",
        "aggregate_spec_path": "/r/aggregate_spec.json",
        "run_id": "ml-abcd1234",
        "cmd_sha": "deadbeef",
    }


def test_complete_path_stages_followups(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_pipeline import submit_pipeline

    with (
        mock.patch("hpc_agent.ops.submit_pipeline.submit_and_verify", return_value=_sv_result()),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.verify_submitted",
            return_value={"ok": True, "states": {"123": "running"}},
        ),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.prepare_followup_specs",
            return_value=_followup(),
        ) as m_prep,
    ):
        res = submit_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "complete"
    assert res.needs_decision is False
    assert res.verified is True
    assert res.verify_submitted_ok is True
    assert res.monitor_spec_path.endswith("monitor_spec.json")
    assert res.aggregate_spec_path.endswith("aggregate_spec.json")
    # cmd_sha threaded from job_env; profile from the pipeline spec.
    assert m_prep.call_args.kwargs["cmd_sha"] == "deadbeef"
    assert m_prep.call_args.kwargs["profile"] == "ml"


def test_deduped_short_circuits(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_pipeline import submit_pipeline

    with (
        mock.patch(
            "hpc_agent.ops.submit_pipeline.submit_and_verify",
            return_value=_sv_result(deduped=True, verified=False, job_ids=["999"]),
        ),
        mock.patch("hpc_agent.ops.submit_pipeline.verify_submitted") as m_vs,
        mock.patch("hpc_agent.ops.submit_pipeline.prepare_followup_specs") as m_prep,
    ):
        res = submit_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "deduped"
    assert res.needs_decision is False
    assert res.deduped is True
    m_vs.assert_not_called()  # no health check on a dedup replay
    m_prep.assert_not_called()


def test_canary_failure_never_verifies_or_stages(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_pipeline import submit_pipeline

    with (
        mock.patch(
            "hpc_agent.ops.submit_pipeline.submit_and_verify",
            return_value=_sv_result(verified=False, job_ids=[], failure_kind="dispatcher_failed"),
        ),
        mock.patch("hpc_agent.ops.submit_pipeline.verify_submitted") as m_vs,
        mock.patch("hpc_agent.ops.submit_pipeline.prepare_followup_specs") as m_prep,
    ):
        res = submit_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "canary_failed"
    assert res.needs_decision is True
    assert res.failure_kind == "dispatcher_failed"
    assert res.job_ids == []
    m_vs.assert_not_called()  # main never launched → nothing to health-check
    m_prep.assert_not_called()


def test_verify_submitted_failure_escalates(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_pipeline import submit_pipeline

    with (
        mock.patch("hpc_agent.ops.submit_pipeline.submit_and_verify", return_value=_sv_result()),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.verify_submitted",
            return_value={"ok": False, "error": ["123"], "states": {"123": "Eqw"}},
        ),
        mock.patch("hpc_agent.ops.submit_pipeline.prepare_followup_specs") as m_prep,
    ):
        res = submit_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "verify_submitted_failed"
    assert res.needs_decision is True
    assert res.verify_submitted_ok is False
    assert res.verify_submitted_result["states"]["123"] == "Eqw"
    m_prep.assert_not_called()  # don't pre-stage follow-ups when jobs didn't land


def test_no_canary_submit_is_not_a_canary_failure(tmp_path: Path) -> None:
    """canary=false → submit-and-verify returns verified=False with
    failure_kind=None and the main job_ids populated. That is a SUCCESSFUL
    direct submit, NOT a canary failure — submit-pipeline must fall through to
    the health check and report ``complete`` (verified honestly False), never
    ``canary_failed`` (which would claim 'the main array never launched')."""
    from hpc_agent.ops.submit_pipeline import submit_pipeline

    with (
        mock.patch(
            "hpc_agent.ops.submit_pipeline.submit_and_verify",
            return_value=_sv_result(verified=False, failure_kind=None, job_ids=["123"]),
        ),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.verify_submitted",
            return_value={"ok": True, "states": {"123": "running"}},
        ),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.prepare_followup_specs",
            return_value=_followup(),
        ) as m_prep,
    ):
        res = submit_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "complete"  # NOT canary_failed
    assert res.needs_decision is False
    assert res.verified is False  # honest — no canary ran
    assert res.job_ids == ["123"]  # the main array DID launch
    m_prep.assert_called_once()  # follow-ups staged on the success path


# ─── DAG readiness gate (docs/design/dag-kernel.md) ────────────────────────
#
# The gate fires only when the embedded submit spec declares parents; it is
# mocked at the module seam like the other composed verbs — the validator's
# own behaviour is pinned in tests/ops/validate/test_validate_parents_ready.py.


def _parented_spec() -> SubmitPipelineSpec:
    spec = _pipeline_spec()
    spec.submit.submit.parents = ["parent-run-1"]
    return spec


def _parents_result(*, ready: bool):
    from hpc_agent._wire.validators.validate_parents_ready import (
        ValidateParentsReadyResult,
    )
    from hpc_agent._wire.workflows.validate_campaign import ValidatorFinding

    if ready:
        return ValidateParentsReadyResult(findings=[], parent_states={"parent-run-1": "complete"})
    return ValidateParentsReadyResult(
        findings=[
            ValidatorFinding(
                validator="validate-parents-ready",
                severity="error",
                code="parent_not_terminal",
                message="declared parent 'parent-run-1' has not reached a terminal lifecycle",
                suggested_fix="wait for the parent to finish",
                evidence={"parent_run_id": "parent-run-1", "observed_state": "in_flight"},
            )
        ],
        parent_states={"parent-run-1": "in_flight"},
    )


def test_parents_not_ready_refuses_before_any_submit(tmp_path: Path) -> None:
    """The stale-inputs invariant: a parented spec with a not-ready parent
    returns a typed refusal and the submit half is NEVER invoked."""
    from hpc_agent.ops.submit_pipeline import submit_pipeline

    with (
        mock.patch(
            "hpc_agent.ops.submit_pipeline.validate_parents_ready",
            return_value=_parents_result(ready=False),
        ),
        mock.patch("hpc_agent.ops.submit_pipeline.submit_and_verify") as m_sv,
    ):
        res = submit_pipeline(tmp_path, spec=_parented_spec())

    assert res.stage_reached == "parents_not_ready"
    assert res.needs_decision is True
    assert res.job_ids == []
    assert res.parent_states == {"parent-run-1": "in_flight"}
    assert [f.code for f in res.parents_ready_findings] == ["parent_not_terminal"]
    m_sv.assert_not_called()


def test_ready_parents_fall_through_to_submit(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_pipeline import submit_pipeline

    with (
        mock.patch(
            "hpc_agent.ops.submit_pipeline.validate_parents_ready",
            return_value=_parents_result(ready=True),
        ) as m_validate,
        mock.patch("hpc_agent.ops.submit_pipeline.submit_and_verify", return_value=_sv_result()),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.verify_submitted",
            return_value={"ok": True, "states": {"123": "running"}},
        ),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.prepare_followup_specs",
            return_value=_followup(),
        ),
    ):
        res = submit_pipeline(tmp_path, spec=_parented_spec())

    assert res.stage_reached == "complete"
    assert res.parent_states is None  # gate passed silently; no frontier to render
    m_validate.assert_called_once()


def test_parentless_spec_never_consults_the_gate(tmp_path: Path) -> None:
    """0-parent degeneracy at the pipeline level: the pre-DAG path is
    byte-for-byte unchanged — the validator is not even called."""
    from hpc_agent.ops.submit_pipeline import submit_pipeline

    with (
        mock.patch("hpc_agent.ops.submit_pipeline.validate_parents_ready") as m_validate,
        mock.patch("hpc_agent.ops.submit_pipeline.submit_and_verify", return_value=_sv_result()),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.verify_submitted",
            return_value={"ok": True, "states": {"123": "running"}},
        ),
        mock.patch(
            "hpc_agent.ops.submit_pipeline.prepare_followup_specs",
            return_value=_followup(),
        ),
    ):
        res = submit_pipeline(tmp_path, spec=_pipeline_spec())

    assert res.stage_reached == "complete"
    m_validate.assert_not_called()
