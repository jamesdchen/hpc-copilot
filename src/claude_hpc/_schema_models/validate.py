"""Pydantic models for the ``validate`` validator's wire contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ValidateSpec(BaseModel):
    """Kwargs contract for ``claude_hpc.planning.validate.validate_submission``.

    Probes the scheduler's --test-only mode for the resource ask and
    returns the predicted submission timing. No submit side effect.
    Not yet exposed as a CLI subcommand; consumers call the function
    directly. When the CLI subcommand lands, this schema will become
    the wire contract for ``--spec``.
    """

    model_config = ConfigDict(extra="forbid", title="validate input spec")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    walltime_sec: int = Field(ge=1)
    mem_mb: int = Field(ge=1)
    cpus: int = Field(ge=1)
    constraint: str | None = Field(
        default=None,
        description="GPU type or feature constraint passed to --constraint. Null or '<cpu-only>' selects the CPU-only branch.",
    )
    gpus: int = Field(default=0, ge=0)
    backfill_window_sec: int = Field(
        default=600,
        ge=1,
        description="ETA threshold for the fits_backfill flag.",
    )


class ValidateResult(BaseModel):
    """Data block from ``hpc-mapreduce validate``.

    Wraps the scheduler's --test-only response into a structured
    timing prediction.
    """

    model_config = ConfigDict(extra="forbid", title="validate output (data block)")

    profile: str
    cluster: str
    scheduler: str
    estimated_start_iso: str | None = Field(
        description="ISO-8601 UTC timestamp when scheduler predicts start.",
    )
    predicted_eta_sec: int | None = Field(
        ge=0,
        description="Seconds from now until predicted start. None when the probe was unparseable or the scheduler is non-SLURM.",
    )
    fits_backfill: bool
    backfill_window_sec: int = Field(ge=1)
    reason: str
    scheduler_response: str = Field(description="Raw probe stdout/stderr (clamped to 2000 chars).")
