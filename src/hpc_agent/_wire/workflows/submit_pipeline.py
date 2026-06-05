"""Pydantic models for the ``submit-pipeline`` workflow primitive.

The deterministic post-resolution submit *spine* as ONE call — the
control-flow-out-of-the-LLM move applied to ``worker_prompts/submit.md``
Steps 7-8 → 9-10. Those steps are mechanical: run the canary-gated submit,
confirm the jobs landed cleanly, pre-stage the follow-up specs, and branch
on each envelope. ``submit-pipeline`` runs that branch logic in code and
reports a single typed ``stage_reached`` outcome, so the agent stops
hand-walking (and hand-branching) the four verbs.

Composition (all ``ops``-subject verbs, so no cross-subject import):

    submit-and-verify  →  verify-submitted  →  prepare-followup-specs

The genuine judgement points (axis classification, entry-point, env
selection) stay UPSTREAM of this spine as escalations — this composite is
what runs once every input is resolved. It is purely **additive**: the agent
may adopt it in place of hand-walking the steps, but the per-verb path keeps
working unchanged. The campaign-only ``validate-campaign`` gate is left out
deliberately (it lives in the ``meta`` subject and is campaign-specific).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec


class SubmitPipelineSpec(BaseModel):
    """Spec passed to ``hpc-agent submit-pipeline --spec <file>``."""

    model_config = ConfigDict(extra="forbid", title="submit-pipeline input spec")

    submit: SubmitAndVerifySpec = Field(
        description=(
            "The canary-gated submit (embeds the submit-flow spec under "
            "``submit.submit`` plus the verify-canary params). submit-pipeline "
            "wraps it with the post-qsub health check + follow-up staging."
        ),
    )
    profile: str | None = Field(
        default=None,
        description=(
            "Run profile (run_name) forwarded to prepare-followup-specs. None "
            "falls back to the embedded submit spec's own ``profile``."
        ),
    )


class SubmitPipelineResult(BaseModel):
    """Shape of the ``data`` field on a ``submit-pipeline`` envelope.

    ``stage_reached`` is the deterministic dispatch the agent used to walk by
    hand. ``needs_decision`` flags the gate failures that require a caller
    decision — the decision-as-data shape (#231): the pipeline ran every
    deterministic branch; only the genuine judgement is handed back.
    """

    model_config = ConfigDict(extra="forbid", title="submit-pipeline output data")

    stage_reached: Literal[
        "deduped",
        "canary_failed",
        "verify_submitted_failed",
        "complete",
    ] = Field(description="Which stage the pipeline reached / stopped at.")
    needs_decision: bool = Field(
        description=(
            "True for the gate failures (canary / verify-submitted) that need a "
            "caller decision; False for the clean terminal stages (complete / deduped)."
        ),
    )
    reason: str = Field(description="Human-readable summary of the outcome / what must be decided.")
    run_id: str | None = Field(default=None)
    job_ids: list[str] = Field(
        default_factory=list,
        description="Main array job ids; empty unless the main array launched.",
    )
    deduped: bool = Field(
        default=False, description="True when the submit half was a deduped replay."
    )
    verified: bool = Field(
        default=False,
        description="True iff the canary was verified and the main array launched (#160).",
    )
    failure_kind: str | None = Field(
        default=None,
        description="Canary failure kind on ``stage_reached='canary_failed'``; else None.",
    )
    verify_submitted_ok: bool | None = Field(
        default=None,
        description="Post-qsub scheduler health; None when that stage was not reached.",
    )
    monitor_spec_path: str | None = Field(
        default=None,
        description="Pre-staged monitor spec path (set on the complete stage).",
    )
    aggregate_spec_path: str | None = Field(
        default=None,
        description="Pre-staged aggregate spec path (set on the complete stage).",
    )
    verify_submitted_result: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The verify-submitted ``data`` on a ``verify_submitted_failed`` stage "
            "(the error/held/missing job ids + states the caller surfaces)."
        ),
    )
