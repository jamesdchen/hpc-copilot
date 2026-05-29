"""Pydantic model for the ``verify-submitted`` query's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VerifySubmittedResult(BaseModel):
    """Per-job scheduler state for a just-submitted array (#157).

    Answers Step 8b's question — "is every submitted job queued/running and
    NOT in an error/held state?" — through a verb instead of raw
    ``ssh … qstat``. The caller branches on ``ok``; ``states`` carries the raw
    per-job token for any case the alive/error/held classification doesn't
    cover.
    """

    model_config = ConfigDict(extra="forbid", title="verify-submitted output")

    ok: bool = Field(
        description="True iff no submitted job is in a scheduler error or held state.",
    )
    states: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "job_id -> raw scheduler state token "
            "(e.g. SGE 'Eqw'/'r', SLURM 'RUNNING'/'PENDING')."
        ),
    )
    healthy: list[str] = Field(
        default_factory=list,
        description="Job ids in a healthy alive state (queued / running).",
    )
    error: list[str] = Field(
        default_factory=list,
        description="Job ids in a scheduler error state (e.g. SGE Eqw).",
    )
    held: list[str] = Field(
        default_factory=list,
        description="Job ids held by the scheduler.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description=(
            "Submitted job ids not present in the scheduler queue "
            "(already terminal/gone, or never landed)."
        ),
    )
    details: str = Field(
        description="One-line human-readable summary the caller can surface.",
    )
