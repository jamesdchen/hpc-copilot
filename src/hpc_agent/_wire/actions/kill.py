"""Pydantic models for the ``kill`` mutator (§5 first-class kill semantics).

``kill`` journals the intent, attempts scheduler cancellation through the
backend seam (if one exists), verifies against the scheduler, and reports the
honest "N requested, N confirmed gone" count. Request → journaled → verified.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hpc_agent._wire._shared import RunIdStrict, Scheduler, SchedulerJobId

# The submit-side task_range grammar: a comma list of ``n`` / ``n-m`` / ``n-m:s``
# tokens (``"4,8,13-15"``). Cancel reuses this SAME vocabulary so submit and
# cancel speak one range language (SPEC §2, Δ4).
_TASK_RANGE_RE = re.compile(r"^\d+(?:-\d+(?::\d+)?)?(?:,\d+(?:-\d+(?::\d+)?)?)*$")


class KillSpec(BaseModel):
    """Input spec for the ``kill`` verb."""

    model_config = ConfigDict(extra="forbid", title="kill input spec")

    run_id: RunIdStrict = Field(description="The run whose scheduler jobs should be killed.")
    scheduler: Scheduler = Field(
        description=(
            "Backend/scheduler name — needed to query the run's alive job IDs and "
            "to build the cancel command dispatched through the backend seam."
        ),
    )
    task_range: str | None = Field(
        default=None,
        description=(
            "Optional scheduler array-index expression ('4,8,13-15', the same "
            "grammar as the submit task_range) scoping the kill to those array "
            "indices — a PARTIAL, range-scoped cancel that leaves the run "
            "in flight (SGE 'qdel <id> -t <range>', SLURM 'scancel <id>_[<range>]'). "
            "Omitted = whole-run kill."
        ),
    )

    @field_validator("task_range")
    @classmethod
    def _validate_task_range(cls, v: str | None) -> str | None:
        """Refuse a task_range that is not a well-formed array-index expression."""
        if v is None:
            return v
        if not _TASK_RANGE_RE.match(v):
            raise ValueError(
                "task_range must be a scheduler array expression like '4,8,13-15' "
                f"(a comma list of n / n-m / n-m:step), got {v!r}"
            )
        return v


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
