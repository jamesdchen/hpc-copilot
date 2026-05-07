"""Pydantic model for the ``find-prior-run`` query atom's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FindPriorRunResult(BaseModel):
    """Resume-detection lookup result.

    found=False distinguishes 'no prior run' from 'prior run is
    orphan' (is_orphan=True signals a half-baked sidecar). The
    prior_run_id field is named with a 'prior_' prefix so the
    schema-defs consistency check (which forbids nullable run_id)
    doesn't trip.
    """

    model_config = ConfigDict(title="find-prior-run output")

    found: bool
    prior_run_id: str | None = Field(
        description="The matching prior run's run_id, or null when found=False.",
    )
    is_orphan: bool
    status: str | None
    age_sec: int | None = Field(ge=0)
    profile: str | None
    cluster: str | None
    job_ids: list[str]
    campaign_id: str | None
    submitted_at: str | None
