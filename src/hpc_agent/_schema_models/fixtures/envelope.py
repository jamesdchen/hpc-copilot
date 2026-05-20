"""Pydantic models for the universal CLI envelope (success/error variants)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from hpc_agent._schema_models._shared import ErrorCode


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


class ErrorEnvelope(BaseModel):
    """The ``ok=false`` shape of every hpc-agent stdout response."""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[False]
    error_code: ErrorCode
    message: str
    category: Literal["user", "cluster", "network", "internal"]
    retry_safe: bool
    remediation: str | None = None


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
