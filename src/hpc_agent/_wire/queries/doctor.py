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


class ParkedRunNote(BaseModel):
    """One live run legitimately awaiting a human decision (§5 "parked ≠ stalled").

    A parked run carries a ``pending_decision`` marker — a ``block-drive`` span
    reached a block's y/nudge boundary and is waiting on the human. It is NOT a
    stalled driver: the read is "awaiting your decision since T", never "driver
    stalled — re-arm?". A parked run never appears in ``DoctorResult.stalled``.
    """

    model_config = ConfigDict(extra="forbid", title="doctor parked-run note")

    run_id: RunIdStrict
    status: str = Field(description="Journal status of the parked run (always 'in_flight').")
    block: str | None = Field(
        default=None, description="The block whose decision the run is parked on, or null."
    )
    workflow: str | None = Field(
        default=None, description="The workflow the parked block belongs to, or null."
    )
    awaiting_since: str | None = Field(
        default=None,
        description="When the run began awaiting the decision (ISO-8601 UTC), or null.",
    )
    note: str = Field(
        description=(
            "Human-facing read, e.g. 'awaiting your decision since <awaiting_since>'. "
            "A parked driver is not stalled — doctor never proposes re-arming it."
        )
    )


class AdvanceRunProposal(BaseModel):
    """One parked run whose human ``y`` is committed but the driver never advanced.

    The §5 Phase-5 out-of-session case: the run carries a ``pending_decision``
    marker AND its latest committed decision is a ``y`` greenlight, but the
    driver that must consume it is dead (session ended, no OS scheduler). This
    is a STALLED driver — distinct from :class:`StalledRunProposal` (a missed
    *tick* deadline) and from :class:`ParkedRunNote` (still genuinely awaiting
    the human) — so ``doctor`` surfaces it with a DRAFTED re-arm/advance
    proposal. Detection only: ``doctor`` never runs ``block-drive`` itself.
    """

    model_config = ConfigDict(extra="forbid", title="doctor awaiting-advance proposal")

    run_id: RunIdStrict
    status: str = Field(
        description="Journal status of the parked-but-decided run (always 'in_flight')."
    )
    block: str | None = Field(
        default=None, description="The block whose decision was approved but not consumed, or null."
    )
    workflow: str | None = Field(
        default=None, description="The workflow the approved block belongs to, or null."
    )
    awaiting_since: str | None = Field(
        default=None,
        description="When the run began awaiting the (now-committed) decision (ISO-8601 UTC), or null.",
    )
    proposal: str = Field(
        description=(
            "Human-facing DRAFTED re-arm proposal, e.g. 'approved spec committed for "
            "<run_id> block <block> but the driver has not advanced — re-arm ...'. "
            "doctor NEVER enacts it."
        )
    )
    evidence: dict[str, Any] = Field(
        description="Detection evidence (the committed decision's ts/response, awaiting_since, now).",
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
    parked_count: int = Field(
        default=0, description="Number of live runs parked on a human decision (§5)."
    )
    parked: list[ParkedRunNote] = Field(
        default_factory=list,
        description=(
            "One entry per run awaiting a human decision — distinct from stalled; "
            "doctor surfaces the wait, never a re-arm proposal."
        ),
    )
    awaiting_advance_count: int = Field(
        default=0,
        description=(
            "Number of parked runs whose human 'y' is committed but the driver "
            "died before advancing (§5 Phase-5) — stalled drivers, surfaced with a "
            "re-arm proposal."
        ),
    )
    awaiting_advance: list[AdvanceRunProposal] = Field(
        default_factory=list,
        description=(
            "One entry per committed-but-unadvanced run — a driver that must be "
            "re-armed to consume an already-approved decision. Distinct from "
            "'parked' (still awaiting the human): doctor drafts the re-arm, never "
            "enacts it."
        ),
    )
