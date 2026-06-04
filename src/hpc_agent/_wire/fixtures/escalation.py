"""Pydantic model for the unified escalation / decision block (#231).

The single *"needs a decision"* shape. It rides on the existing binary
envelope (:class:`SuccessEnvelope` | :class:`ErrorEnvelope`) as one
optional, typed field — present on *either* outcome, because **"needs a
decision" is orthogonal to success/failure**: a converged-but-over-budget
campaign is a *success* that needs a decision; a service/task failure is a
*failure* that needs one. Modelling it as a third wire state (a tristate
``ok``) would force those two facts onto one axis and discard one of them.

This block resolves the three fragmented escalation shapes that exist
today — :class:`hpc_agent._wire.fixtures.envelope.ErrorEnvelope`, the
``campaign-advance`` decision dict
(:func:`hpc_agent.meta.campaign.atoms.advance.campaign_advance`), and
:class:`hpc_agent._wire.spawn_contract.WorkerReport` — into one socket the
deterministic resolver (#234) escalates through and the staging quota-gate
(#232) reuses. **Decision-as-data:** the wire contract stays binary; this
block is the second axis, and a consumer that only reads ``ok``/``data``/
``error`` keeps working untouched.

See issue #231's "Resolved design" section for why this is preferred over
a tristate envelope (rejected — conflates the axes) or a separate decision
channel (deferred — a whole transport+persistence surface; this typed
block becomes that channel's message body verbatim if volume ever demands
it).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire.fixtures.failure_features import FailureFeatures
from hpc_agent._wire.spawn_contract import DecidedBy


class CandidateAction(BaseModel):
    """One action a decision-maker may choose for an escalated cluster.

    The deterministic resolver (#234) offers candidates it could not
    rank with confidence; the agentic layer (or a human) picks one. A
    ``decided_by="code"`` escalation may carry a single recommended
    candidate (a deterministic recommendation surfaced for confirmation,
    e.g. ``campaign-advance``'s computed decision); a
    ``decided_by="judgement"`` escalation carries the option set the LLM
    reasons over.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        description=(
            "The action verb, e.g. 'increase-mem-per-gpu', 'reshard', "
            "'retry-on-different-node', 'continue', 'stop_converged'. Mirrors the "
            "'suggested_fix.action' vocabulary the failure-signature CATALOG emits "
            "and the campaign-advance decision values."
        ),
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for the action, e.g. {'factor': 1.5}.",
    )
    rationale: str = Field(
        default="",
        description="Why this candidate is offered (the discriminating evidence behind it).",
    )
    source: Literal["catalog", "policy", "history", "judgement"] | None = Field(
        default=None,
        description=(
            "Where the candidate came from: 'catalog' (signature CATALOG fix), "
            "'policy' (context-keyed retry policy), 'history' (a memoized prior "
            "verdict from recall), or 'judgement' (LLM-proposed)."
        ),
    )


class EscalationCluster(BaseModel):
    """Provenance back to the affected tasks so a verdict fans back out.

    *Cluster to decide once, do not dedup to discard* (#234): the
    escalation carries ONE decision per signature, but keeps the per-task
    refs so the chosen fix applies to each task. ``fingerprint`` is the
    cluster key produced by
    ``ops.recover.runner_failures.cluster_failures_by_fingerprint``.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint: str | None = Field(
        default=None,
        description="The cluster signature (the fingerprint failures were grouped by).",
    )
    run_id: str | None = Field(
        default=None,
        description="The run whose tasks this escalation concerns, when single-run.",
    )
    task_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Per-task refs the verdict fans back out to. Kept inside the cluster so "
            "a single decision re-applies per task rather than being deduped away."
        ),
    )
    wave: int | None = Field(
        default=None,
        description="The wave the affected tasks belong to, when applicable.",
    )


class Escalation(BaseModel):
    """One *"needs a decision"* block attached to an envelope (#231).

    Attached optionally to ``SuccessEnvelope`` (e.g. campaign-advance's
    succeeded-but-decide case) or ``ErrorEnvelope`` (a failure the
    deterministic resolver could not resolve). Evidence + options +
    provenance; it does not itself carry the verdict or the holding state
    (the journal's ``pending_verdict`` state owns that — Phase 2 / #234).
    """

    model_config = ConfigDict(extra="forbid")

    decided_by: DecidedBy = Field(
        description=(
            "The decision seam (reuses spawn_contract's DecidedBy). 'code' = a "
            "deterministic resolver produced this (the block is a recommendation "
            "surfaced for confirmation / audit); 'judgement' = the deterministic "
            "layer could not resolve it and the agentic layer must decide. The "
            "rate of 'judgement' for a given cluster fingerprint is the #234 health "
            "signal that a context-keyed rule should be promoted."
        ),
    )
    reason: str = Field(
        default="",
        description="Human-readable summary of why this escalated / what must be decided.",
    )
    failure_features: FailureFeatures | None = Field(
        default=None,
        description=(
            "The #230 evidence vector — the single inter-layer interface. The "
            "deterministic resolver pattern-matches on it; the agentic layer "
            "reasons over it as its prompt payload. Same evidence, two consumers."
        ),
    )
    candidate_actions: list[CandidateAction] = Field(
        default_factory=list,
        description="The actions the decision-maker may choose among.",
    )
    cluster: EscalationCluster | None = Field(
        default=None,
        description="Provenance back to the affected tasks, so a verdict fans back out per-task.",
    )


def escalation_of(envelope: dict[str, Any]) -> Escalation | None:
    """Extract the escalation block from a raw envelope dict, or ``None``.

    The router contract: a consumer inspects *every* envelope — ``ok=true``
    or ``ok=false`` — for an escalation, so a succeeded-but-needs-a-decision
    case is never silently dropped (the one weakness of an optional field).
    Returns a validated :class:`Escalation` when present, ``None`` when the
    operation needed no decision.
    """
    block = envelope.get("escalation")
    if block is None:
        return None
    return Escalation.model_validate(block)
