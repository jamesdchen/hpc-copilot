"""Pydantic models for the ``campaign-health`` query atom's wire contract."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CampaignHealthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", title="campaign-health input spec")

    campaign_id: str | None = None
    since_iso: str | None = None
    profile: str | None = None
    cluster: str | None = None


class CampaignHealthResult(BaseModel):
    """Structured run-history aggregation.

    The ``suggested_prompt`` field is ready-to-feed-LLM (callers
    append no preamble; pass it directly).
    """

    model_config = ConfigDict(extra="forbid", title="campaign-health output (data block)")

    campaign_id: str | None
    since_iso: str | None
    n_runs: int = Field(ge=0)
    n_complete: int = Field(ge=0)
    n_failed: int = Field(ge=0)
    walltime_cliff_rate: dict[str, float] = Field(
        description="Per-bucket cliff-rate fractions in [0, 1].",
    )
    queue_wait_quantiles: dict[str, dict[str, Any]]
    failure_breakdown: dict[str, int]
    gpu_utilization: dict[str, dict[str, Any]]
    suggested_prompt: str
