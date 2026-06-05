"""``submit-pipeline``: the deterministic post-resolution submit spine in one call.

Folds ``worker_prompts/submit.md`` Steps 7-8 → 9-10 — the canary-gated
submit, the post-qsub health check, and the follow-up-spec pre-staging —
into ONE workflow primitive. Those steps are mechanical: each is a verb call
followed by a deterministic branch on its envelope. ``submit-pipeline`` runs
the branches in code and returns a single typed ``stage_reached`` outcome, so
the agent stops hand-walking (and hand-branching) the verbs.

This is the control-flow-out-of-the-LLM pattern ``submit-and-verify`` started,
extended one ring outward: where ``submit-and-verify`` absorbed the
submit→verify→submit canary sub-loop, ``submit-pipeline`` absorbs the
validate-gate-free spine around it.

Composition (all ``ops``-subject, so no cross-subject import):

    submit-and-verify  →  verify-submitted  →  prepare-followup-specs

Escalation-as-data (#231): the only outcomes that set ``needs_decision=True``
are the genuine gate failures — a canary that failed verification, or
submitted jobs that did not land clean. Everything else (``deduped`` /
``complete``) is a terminal the agent just reports. The upstream judgement
points (axis classification, entry-point, env) are NOT in this spine; they
escalate before it runs.

**Additive.** This primitive does not replace the per-verb worker-prompt
path — it is a new verb the prompt may adopt. Nothing breaks if it is not yet
wired in, which is why it ships before the prompt is restructured.

I/O contracts:

* Input: ``schemas/submit_pipeline.input.json`` (from ``SubmitPipelineSpec``).
* Output: ``schemas/submit_pipeline.output.json`` (from ``SubmitPipelineResult``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.submit_pipeline import (
    SubmitPipelineResult,
    SubmitPipelineSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.prepare_followup_specs import prepare_followup_specs
from hpc_agent.ops.submit_and_verify import submit_and_verify
from hpc_agent.ops.verify_submitted import verify_submitted

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["submit_pipeline"]


@primitive(
    name="submit-pipeline",
    verb="workflow",
    composes=["submit-and-verify", "verify-submitted", "prepare-followup-specs"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect("ssh", "<cluster> (canary poll + post-qsub state)"),
        SideEffect("writes-followup-specs", "<experiment_dir>/{monitor,aggregate}_spec.json"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="submit.submit.run_id",
    cli=CliShape(
        help=(
            "Deterministic submit spine in one call: submit-and-verify (the "
            "canary gate) → verify-submitted (post-qsub health) → "
            "prepare-followup-specs. Reports one typed stage_reached outcome; "
            "sets needs_decision only on the genuine gate failures."
        ),
        spec_arg=True,
        spec_model=SubmitPipelineSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_pipeline"),
    ),
    agent_facing=True,
)
def submit_pipeline(experiment_dir: Path, *, spec: SubmitPipelineSpec) -> SubmitPipelineResult:
    """Run the canary-gated submit, confirm the jobs landed, pre-stage follow-ups.

    Returns a single :class:`SubmitPipelineResult`; ``stage_reached`` is the
    deterministic dispatch the caller branches on, and ``needs_decision`` is
    set only on the canary / verify-submitted gate failures (the rest is
    terminal). A failed canary returns with ``job_ids`` empty — the main array
    never launched (#160) — and a failed post-qsub health check returns the
    offending job states under ``verify_submitted_result``.
    """
    # 1. Canary-gated submit (the #160 two-phase gate, run in code).
    sv = submit_and_verify(experiment_dir, spec=spec.submit)

    if sv.deduped:
        return SubmitPipelineResult(
            stage_reached="deduped",
            needs_decision=False,
            reason=(
                "run already on the journal (deduped replay); switch to the status "
                "workflow — do NOT re-submit."
            ),
            run_id=sv.run_id,
            job_ids=list(sv.job_ids),
            deduped=True,
        )

    # A canary that RAN and failed the gate (failure_kind set) stops here — the
    # main array never launched (#160). A no-canary submit (spec canary=false)
    # returns verified=False with failure_kind=None and the main job_ids already
    # populated — that is NOT a failure, so it falls through to the health check.
    if not sv.verified and sv.failure_kind is not None:
        return SubmitPipelineResult(
            stage_reached="canary_failed",
            needs_decision=True,
            reason=(
                f"canary failed verification (failure_kind={sv.failure_kind}); the "
                "main array never launched. Fix the dispatch and re-invoke."
            ),
            run_id=sv.run_id,
            failure_kind=str(sv.failure_kind),
            verified=False,
        )

    # 2. Post-qsub health: a job id is necessary but not sufficient (Eqw / held).
    vs = verify_submitted(experiment_dir, run_id=sv.run_id)
    if not vs.get("ok", False):
        return SubmitPipelineResult(
            stage_reached="verify_submitted_failed",
            needs_decision=True,
            reason=(
                "submitted jobs did not all land queued/running (error/held/missing); "
                "do NOT proceed to monitor — surface the offending job ids and stop."
            ),
            run_id=sv.run_id,
            job_ids=list(sv.job_ids),
            verified=sv.verified,
            verify_submitted_ok=False,
            verify_submitted_result=vs,
        )

    # 3. Pre-stage the follow-up specs (harmless; always on the success path, #278).
    cmd_sha = (spec.submit.submit.job_env or {}).get("HPC_CMD_SHA")
    followup = prepare_followup_specs(
        experiment_dir=str(experiment_dir),
        run_id=sv.run_id,
        cmd_sha=cmd_sha,
        profile=spec.profile or spec.submit.submit.profile,
    )

    return SubmitPipelineResult(
        stage_reached="complete",
        needs_decision=False,
        reason=(
            "submitted, jobs healthy, follow-up specs staged"
            + (" (canary verified)." if sv.verified else " (no canary requested).")
        ),
        run_id=sv.run_id,
        job_ids=list(sv.job_ids),
        verified=sv.verified,
        verify_submitted_ok=True,
        monitor_spec_path=followup.get("monitor_spec_path"),
        aggregate_spec_path=followup.get("aggregate_spec_path"),
    )
