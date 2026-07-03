"""Pydantic models for the ``kill`` mutator (§5 first-class kill semantics).

``kill`` journals the intent, attempts scheduler cancellation through the
backend seam (if one exists), verifies against the scheduler, and reports the
honest "N requested, N confirmed gone" count. Request → journaled → verified.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict, Scheduler, SchedulerJobId


class KillSpec(BaseModel):
    """Input spec for the ``kill`` verb."""

    model_config = ConfigDict(extra="forbid", title="kill input spec")

    run_id: RunIdStrict = Field(description="The run whose scheduler jobs should be killed.")
    scheduler: Scheduler = Field(
        description=(
            "Backend/scheduler name — needed to query the run's alive job IDs and "
            "(when the seam grows one) to build the cancel command."
        ),
    )


class KillResult(BaseModel):
    """Shape of the ``data`` field on a ``kill`` envelope."""

    model_config = ConfigDict(extra="forbid", title="kill output data")

    run_id: RunIdStrict
    requested_job_ids: list[SchedulerJobId] = Field(
        default_factory=list, description="The job IDs the kill was requested against."
    )
    confirmed_gone_job_ids: list[SchedulerJobId] = Field(
        default_factory=list,
        description="Subset verified no longer known to the scheduler after the request.",
    )
    still_alive_job_ids: list[SchedulerJobId] = Field(
        default_factory=list,
        description="Subset still known to the scheduler (e.g. no cancel affordance, or a "
        "verification failure — counted honestly as NOT gone).",
    )
    requested_count: int = Field(description="len(requested_job_ids).")
    confirmed_count: int = Field(description="len(confirmed_gone_job_ids).")
    backend_cancel_attempted: bool = Field(
        description="Whether a scheduler cancel command was actually dispatched through the seam.",
    )
    backend_cancel_available: bool = Field(
        description=(
            "Whether the backend seam exposes a cancel affordance at all. False today "
            "for every built-in backend — the missing cancel is an integration item."
        ),
    )
    summary: str = Field(description="Honest headline, e.g. '3 requested, 0 confirmed gone'.")
    requested_at: str = Field(description="When the kill intent was journaled (ISO-8601 UTC).")
    confirmed_at: str = Field(description="When the verified-gone subset was journaled (ISO-8601).")
    settled: bool = Field(
        default=False,
        description=(
            "True when a FULL kill (everything confirmed gone, nothing still "
            "alive) was settled through reconcile — the journal marked terminal "
            "and the terminal harvest fired exactly once. False for a partial "
            "kill (run still live) or when the best-effort reconcile settle "
            "failed (the kill result still stands)."
        ),
    )
