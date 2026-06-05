"""``campaign-run``: one campaign iteration's submit→monitor→aggregate spine.

Folds the deterministic three-stage iteration spine into ONE call — the
control-flow-out-of-the-LLM pattern applied one ring further out than
``submit-pipeline`` / ``status-pipeline``. Those each fold a single
workflow's spine; ``campaign-run`` is a composite-of-composites that chains
them:

    submit-pipeline  →  status-pipeline  →  aggregate-flow

Each stage is a verb call followed by a deterministic branch on its typed
outcome. ``campaign-run`` runs those branches in code and returns a single
``stage_reached``, so the driver stops hand-walking (and hand-branching) the
three composites for every iteration.

Scope: ONE iteration's spine only. The campaign CURSOR / manifest
advancement — advance vs. converge, budget accounting, target checks — is
NOT part of this composite; those stay judgement escalations owned by the
campaign driver. ``campaign-run`` runs the deterministic remainder and hands
the genuine decisions back as data.

Escalation-as-data (#231): ``needs_decision=True`` only on the failure /
budget stages (``submit_failed`` / ``run_failed`` / ``run_timeout`` /
``run_abandoned`` / ``aggregate_failed``). ``complete`` is the clean terminal
the driver proceeds from to its own advance/converge judgement.

**Additive.** Does not replace the per-composite path — it is a new verb the
driver may adopt. Nothing breaks if it is not yet wired in.

I/O contracts:

* Input: ``schemas/campaign_run.input.json`` (from ``CampaignRunSpec``).
* Output: ``schemas/campaign_run.output.json`` (from ``CampaignRunResult``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.campaign_run import (
    CampaignRunResult,
    CampaignRunSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.ops.status_pipeline import status_pipeline
from hpc_agent.ops.submit_pipeline import submit_pipeline

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["campaign_run"]


@primitive(
    name="campaign-run",
    verb="workflow",
    composes=["submit-pipeline", "status-pipeline", "aggregate-flow"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect("ssh", "<cluster> (canary poll + status polls + aggregate pull)"),
        SideEffect(
            "writes-aggregate-output",
            "<experiment_dir>/_aggregated/<run_id>/ (+ follow-up specs, tick log)",
        ),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="campaign.run.run_id",
    cli=CliShape(
        help=(
            "One campaign iteration's spine in one call: submit-pipeline → "
            "status-pipeline → aggregate-flow. Reports one typed stage_reached "
            "outcome; sets needs_decision only on the failure / budget stages. "
            "Does NOT advance the campaign cursor — that stays a driver judgement."
        ),
        spec_arg=True,
        spec_model=CampaignRunSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="campaign_run"),
    ),
    agent_facing=True,
)
def campaign_run(experiment_dir: Path, *, spec: CampaignRunSpec) -> CampaignRunResult:
    """Run one iteration's submit→monitor→aggregate spine; return one outcome.

    Returns a single :class:`CampaignRunResult`; ``stage_reached`` is the
    deterministic dispatch over the three sub-stages, and ``needs_decision``
    is set only on the failure / budget stages. ``complete`` is the clean
    terminal — the driver's advance/converge judgement for the campaign
    CURSOR is NOT part of this composite and runs after it returns.
    """
    cid = spec.campaign_id

    # 1. Submit spine. A gate failure (canary / post-qsub health) stops before
    #    we ever monitor — the array never landed clean, so there is nothing to
    #    watch. A `deduped` outcome still proceeds: the run already exists/live,
    #    so we monitor the existing run.
    sp = submit_pipeline(experiment_dir, spec=spec.submit)
    if sp.stage_reached in {"canary_failed", "verify_submitted_failed"}:
        return CampaignRunResult(
            stage_reached="submit_failed",
            needs_decision=True,
            reason=(
                f"submit spine stopped at {sp.stage_reached!r}: {sp.reason} "
                "Do NOT monitor — fix the dispatch and re-invoke this iteration."
            ),
            campaign_id=cid,
            run_id=sp.run_id,
            job_ids=list(sp.job_ids),
        )

    # Thread the run we ACTUALLY submitted (sp.run_id) into the monitor + aggregate
    # specs. The per-step loop passes "the returned run_id" to monitor-flow /
    # aggregate-flow; doing the same here makes the composite robust instead of
    # trusting the caller to have pre-aligned all three sub-specs' run_id — a
    # mismatch would otherwise monitor / aggregate the WRONG run silently. On a
    # `deduped` submit, sp.run_id is the already-live run, which is what we want.
    run_id = sp.run_id
    status_spec = spec.status.model_copy(
        update={"monitor": spec.status.monitor.model_copy(update={"run_id": run_id})}
    )
    aggregate_spec = spec.aggregate.model_copy(update={"run_id": run_id})

    # 2. Monitor spine. Only a `complete` lifecycle proceeds to aggregate;
    #    failed / abandoned / timeout each stop with needs_decision=True (we
    #    cannot aggregate a run that did not complete). timeout is the budget
    #    case: the jobs are still live, so the driver re-invokes to keep
    #    watching — surfaced as run_failed would be wrong, so it gets its own
    #    clean non-aggregated outcome under the abandoned/failed family with a
    #    re-invoke reason.
    st = status_pipeline(experiment_dir, spec=status_spec)
    if st.stage_reached == "failed":
        return CampaignRunResult(
            stage_reached="run_failed",
            needs_decision=True,
            reason=(
                f"run reached a terminal failed state: {st.reason} "
                "Classify the failed tasks (recoverable vs not) and decide "
                "resubmit-failed / reconcile-journal before re-invoking."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
        )
    if st.stage_reached == "abandoned":
        return CampaignRunResult(
            stage_reached="run_abandoned",
            needs_decision=True,
            reason=(
                f"recorded jobs are no longer known to the scheduler: {st.reason} "
                "Run reconcile-journal to confirm before re-submitting this iteration."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
        )
    if st.stage_reached == "timeout":
        # Budget elapsed with the cluster jobs still live — we can't aggregate
        # yet, but nothing failed. Its own `run_timeout` stage (NOT run_failed)
        # keeps the distinction honest; needs_decision=True so the driver
        # re-invokes to keep watching.
        return CampaignRunResult(
            stage_reached="run_timeout",
            needs_decision=True,
            reason=(
                "wall-clock budget elapsed with the cluster jobs still live; the "
                "run did not reach complete, so this iteration cannot aggregate "
                "yet. Re-invoke to keep watching (nothing failed — budget only)."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
        )

    # st.stage_reached == "complete" — the only lifecycle that aggregates.

    # 3. Aggregate spine. A raised error (combiner/outputs/ssh) OR a partial
    #    success (non-null escalation_reason: some wave did not combine) is an
    #    aggregate_failed escalation — the caller inspects failed_waves and
    #    decides whether the partial is acceptable. A clean result is the
    #    iteration's terminal `complete`.
    try:
        agg = aggregate_flow(experiment_dir, spec=aggregate_spec)
    except errors.HpcError as exc:
        return CampaignRunResult(
            stage_reached="aggregate_failed",
            needs_decision=True,
            reason=(
                f"aggregate spine errored ({type(exc).__name__}: {exc}). The run "
                "completed but its results did not aggregate; inspect and re-invoke "
                "the aggregate step."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
        )

    if agg.escalation_reason is not None:
        return CampaignRunResult(
            stage_reached="aggregate_failed",
            needs_decision=True,
            reason=(
                f"aggregate spine returned a partial result "
                f"(escalation_reason={agg.escalation_reason!r}); some waves did not "
                "combine. Inspect failed_waves and decide whether the partial "
                "aggregate is acceptable."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
            aggregate_result=agg.to_envelope_data(),
        )

    return CampaignRunResult(
        stage_reached="complete",
        needs_decision=False,
        reason=(
            "iteration spine complete: submitted, run reached complete, results "
            "aggregated cleanly. Hand back to the driver for advance/converge."
        ),
        campaign_id=cid,
        run_id=st.run_id,
        job_ids=list(sp.job_ids),
        lifecycle_state=st.lifecycle_state,
        aggregate_result=agg.to_envelope_data(),
    )
