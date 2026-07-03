"""Pydantic models for the ``doctor`` watchdog query (§5).

``doctor`` scans live runs for a missed driver-tick deadline and surfaces each
as a DRAFTED recovery proposal — detection only, it never restarts or re-arms
anything (design §5: "The watchdog never restarts anything").
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class DoctorSpec(BaseModel):
    """Input spec for the ``doctor`` verb."""

    model_config = ConfigDict(extra="forbid", title="doctor input spec")

    now: str | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 UTC 'now' override for deterministic testing. "
            "When omitted, the current time is used. A stalled run is one whose "
            "next_tick_due is before this instant."
        ),
    )
    notify: bool = Field(
        default=False,
        description=(
            "Opt-in: when True and stalled runs are found, raise an OS "
            "notification carrying the drafted re-arm proposal (notify only, "
            "never acts). Default False keeps the plain in-session verb "
            "unchanged; the OS-scheduled installer bakes notify=true into its "
            "durable spec so the out-of-session scan alerts instead of printing "
            "JSON nobody reads (design §5)."
        ),
    )


class StalledRunProposal(BaseModel):
    """One stalled run + its drafted (never-enacted) recovery proposal."""

    model_config = ConfigDict(extra="forbid", title="doctor stalled-run proposal")

    run_id: RunIdStrict
    status: str = Field(description="Journal status of the stalled run (always 'in_flight').")
    last_tick_at: str | None = Field(
        default=None, description="When the driver last ticked (ISO-8601 UTC), or null if unknown."
    )
    next_tick_due: str | None = Field(
        default=None,
        description="The missed deadline: when the next tick was due (ISO-8601 UTC).",
    )
    cluster: str | None = Field(default=None, description="Cluster the run was submitted to.")
    ssh_target: str | None = Field(default=None, description="SSH target for the run's cluster.")
    proposal: str = Field(
        description=(
            "Human-facing DRAFTED recovery proposal, e.g. 'driver stalled since "
            "<last_tick_at>, status in_flight, re-arm?'. Surfaced for a y/nudge "
            "decision — doctor NEVER enacts it."
        )
    )
    evidence: dict[str, Any] = Field(
        description="The detection evidence (deadline, now, seconds overdue) behind the proposal.",
    )


class DoctorResult(BaseModel):
    """Shape of the ``data`` field on a ``doctor`` envelope."""

    model_config = ConfigDict(extra="forbid", title="doctor output data")

    now: str = Field(description="The instant the scan was evaluated against (ISO-8601 UTC).")
    stalled_count: int = Field(description="Number of live runs past their tick deadline.")
    stalled: list[StalledRunProposal] = Field(
        default_factory=list,
        description="One entry per stalled run, each with a drafted recovery proposal.",
    )
