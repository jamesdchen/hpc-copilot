"""Pydantic models for the ``campaign-run`` workflow primitive.

One campaign iteration's deterministic *spine* as ONE call — the
control-flow-out-of-the-LLM move applied one ring further out than
``submit-pipeline`` / ``status-pipeline``. Where those each fold a single
workflow's spine, ``campaign-run`` folds the three-stage iteration spine —
submit, then monitor, then aggregate — into one composite-of-composites.

Composition (all ``ops``-subject verbs, so no cross-subject import):

    submit-pipeline  →  status-pipeline  →  aggregate-flow

Scope: ONE iteration's spine only. The campaign CURSOR / manifest
advancement — advance vs. converge, budget accounting, target checks — is
NOT part of this composite. Those stay judgement escalations owned by the
campaign driver. ``campaign-run`` runs the deterministic submit→monitor→
aggregate remainder for a single iteration and hands the genuine decisions
back as data.

Escalation-as-data (#231): ``needs_decision=True`` only on the failure /
budget stages (``submit_failed`` / ``run_failed`` / ``run_abandoned`` /
``aggregate_failed``); ``complete`` is the clean terminal the driver
proceeds from (to its own advance/converge judgement).

**Additive.** Does not replace the per-composite path — it is a new verb the
driver may adopt. Nothing breaks if it is not yet wired in.

I/O contracts:

* Input: ``schemas/campaign_run.input.json`` (from ``CampaignRunSpec``).
* Output: ``schemas/campaign_run.output.json`` (from ``CampaignRunResult``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent._wire.workflows.status_pipeline import StatusPipelineSpec
from hpc_agent._wire.workflows.submit_pipeline import SubmitPipelineSpec


class CampaignRunSpec(BaseModel):
    """Spec passed to ``hpc-agent campaign-run --spec <file>``.

    Embeds the three sub-composite specs verbatim (no field re-definition):
    the canary-gated submit spine (``submit``), the wait-until-terminal
    status spine (``status``), and the aggregate spine (``aggregate``).
    ``campaign-run`` runs them in sequence and branches on each typed
    outcome, returning one ``stage_reached``.
    """

    model_config = ConfigDict(extra="forbid", title="campaign-run input spec")

    submit: SubmitPipelineSpec = Field(
        description=(
            "The submit spine for this iteration (submit-pipeline input: the "
            "canary-gated submit + post-qsub health + follow-up staging). "
            "campaign-run runs it first; a gate failure stops before monitor."
        ),
    )
    status: StatusPipelineSpec = Field(
        description=(
            "The monitor spine for this iteration (status-pipeline input: the "
            "wait-until-terminal monitor + lifecycle dispatch). Run only after "
            "submit clears; only a `complete` lifecycle proceeds to aggregate."
        ),
    )
    aggregate: AggregateFlowSpec = Field(
        description=(
            "The aggregate spine for this iteration (aggregate-flow input: "
            "ensure-combined + pull + reduce). Run only after the run reaches "
            "`complete`; its outcome decides the terminal stage."
        ),
    )
    campaign_id: str | None = Field(
        default=None,
        description=(
            "Optional iteration tag for this campaign-run, carried through to "
            "the result for the driver's own bookkeeping. campaign-run does NOT "
            "advance any cursor — the tag is pass-through context only."
        ),
    )


class CampaignRunResult(BaseModel):
    """Shape of the ``data`` field on a ``campaign-run`` envelope.

    ``stage_reached`` is the deterministic dispatch over the three sub-stage
    outcomes; ``needs_decision`` flags the stages that hand a genuine
    judgement back (the failure / budget stages). This is the
    decision-as-data shape (#231): the composite ran the whole spine; only
    the irreducible decisions (classify a failure, reconcile an abandoned
    run, re-invoke after budget, inspect a partial aggregate) escalate.

    ``complete`` is the clean terminal — the driver proceeds from it to its
    own advance/converge judgement, which is NOT part of this composite.
    """

    model_config = ConfigDict(extra="forbid", title="campaign-run output data")

    stage_reached: Literal[
        "submit_failed",
        "run_failed",
        "run_timeout",
        "run_abandoned",
        "aggregate_failed",
        "complete",
    ] = Field(description="Which stage of the iteration spine the composite reached / stopped at.")
    needs_decision: bool = Field(
        description=(
            "True for the failure / budget stages (submit_failed / run_failed / "
            "run_timeout / run_abandoned / aggregate_failed) that hand a decision "
            "back; False for the clean `complete` terminal. run_timeout is the "
            "budget case — nothing failed; the driver re-invokes to keep watching."
        ),
    )
    reason: str = Field(description="Human-readable summary of the outcome / what must be decided.")
    campaign_id: str | None = Field(
        default=None,
        description="The iteration tag echoed from the spec (pass-through; null when unset).",
    )
    run_id: str | None = Field(
        default=None,
        description=(
            "The run_id threaded from the submit / status sub-stages; null on early failure."
        ),
    )
    job_ids: list[str] = Field(
        default_factory=list,
        description="Main array job ids from the submit sub-stage; empty unless the array launched.",
    )
    lifecycle_state: str | None = Field(
        default=None,
        description=(
            "The terminal lifecycle_state from the status sub-stage "
            "(complete / failed / abandoned / timeout); null when submit stopped first."
        ),
    )
    aggregate_result: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The aggregate-flow `data` summary on a `complete` stage (combined_waves, "
            "failed_waves, aggregated_metrics, etc.); null when aggregate was not reached "
            "or did not produce a clean result."
        ),
    )
