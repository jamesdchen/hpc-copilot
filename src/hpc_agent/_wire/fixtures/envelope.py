"""Pydantic models for the universal CLI envelope (success/error variants)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from hpc_agent._wire._shared import ErrorCode
from hpc_agent._wire.fixtures.escalation import Escalation
from hpc_agent._wire.fixtures.failure_features import FailureFeatures


class _PartialError(BaseModel):
    """One soft per-source failure surfaced on a successful envelope.

    Distinct from ``data.errors``: this key documents *cluster-side*
    degradations (qhost timed out, one of N nodes unreachable, sacct
    unavailable) that the planner considered and chose to tolerate.
    Operations that genuinely failed should set ``ok: false`` instead.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        description="Machine-readable category, e.g. 'qhost_failed', 'scontrol_failed', 'qstat_unavailable', 'qacct_unavailable', 'malformed_row'.",
    )
    detail: str


class SuccessEnvelope(BaseModel):
    """The ``ok=true`` shape of every hpc-agent stdout response."""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[True]
    idempotent: bool
    data: dict[str, Any]
    partial_errors: list[_PartialError] | None = Field(
        default=None,
        description=(
            "Optional top-level array surfacing soft per-source "
            "failures while the operation as a whole succeeded."
        ),
    )
    escalation: Escalation | None = Field(
        default=None,
        description=(
            "Optional 'needs a decision' block (#231). Present on a SUCCESS when "
            "the operation succeeded but a decision is still required — e.g. "
            "campaign-advance reaching 'stop_converged' / 'stop_over_budget', or a "
            "stage-out hitting a quota gate. 'Needs a decision' is orthogonal to "
            "ok, so it rides as data on either envelope rather than as a third wire "
            "state. A consumer that ignores it sees an ordinary success."
        ),
    )


class ErrorEnvelope(BaseModel):
    """The ``ok=false`` shape of every hpc-agent stdout response."""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[False]
    error_code: ErrorCode
    message: str
    category: Literal["user", "cluster", "network", "internal"]
    retry_safe: bool
    remediation: str | None = None
    failure_features: FailureFeatures | None = Field(
        default=None,
        description=(
            "Optional structured diagnostic evidence for this failure (#230): the "
            "feature set a diagnosis would need (error_class, resource_spec, "
            "temporal_context, liveness_vs_correctness, log_tail, probes), "
            "independent of what failed. Evidence only — no recovery behavior; "
            "populated where the operation can supply it. Consumed by deterministic "
            "retry policy and the agentic escalation layer (#234)."
        ),
    )
    escalation: Escalation | None = Field(
        default=None,
        description=(
            "Optional 'needs a decision' block (#231). Present on a FAILURE when the "
            "deterministic resolver (#234) could not resolve it and the agentic layer "
            "must decide; carries the failure_features evidence, the candidate "
            "actions, and the affected-task cluster so a verdict fans back out "
            "per-task. The same block shape appears on SuccessEnvelope — one socket, "
            "both outcomes."
        ),
    )


# Discriminated union over the ``ok`` field. Pydantic emits this as a
# ``oneOf`` keyed on ``ok``'s const value, exactly matching the
# hand-authored envelope.json shape (with $defs orphans dropped — no
# Pydantic-emitted schema references them since each consumer now
# imports shared types from ``_shared.py`` instead).
EnvelopeAdapter: TypeAdapter[SuccessEnvelope | ErrorEnvelope] = TypeAdapter(
    Annotated[
        SuccessEnvelope | ErrorEnvelope,
        Field(discriminator="ok"),
    ]
)
