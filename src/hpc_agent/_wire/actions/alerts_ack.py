"""Pydantic models for the ``alerts-ack`` verb (§5 watchdog alert delivery).

The scheduled ``doctor`` watchdog appends drafted re-arm proposals to
``doctor.alerts.log``; an alert stays "unacknowledged" until a status surface
has shown it to the human once (proving run #3: detection without delivery is
silence). ``alerts-ack`` is the standalone maintenance verb that advances the
acknowledgment watermark — the same watermark the status snapshot advances as a
side effect of ``mark_seen`` — so the human can dismiss surfaced alerts without
running a full snapshot. The log itself is an append-only audit trail and is
never touched.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AlertsAckSpec(BaseModel):
    """Input spec for the ``alerts-ack`` verb."""

    model_config = ConfigDict(extra="forbid", title="alerts-ack input spec")

    up_to_ts: str | None = Field(
        default=None,
        description=(
            "Acknowledge every alert at or before this ISO-8601 UTC instant. When "
            "omitted, acknowledges up to the newest alert currently in the log (or "
            "'now' if the log is empty/unreadable) — the common 'dismiss what I've "
            "seen' case. Monotonic: a watermark already past this instant is left "
            "alone, so a stale call never resurrects acknowledged alerts."
        ),
    )


class AlertsAckResult(BaseModel):
    """Shape of the ``data`` field on an ``alerts-ack`` envelope."""

    model_config = ConfigDict(extra="forbid", title="alerts-ack output data")

    acknowledged_up_to: str = Field(
        description="The ISO-8601 UTC instant the acknowledgment watermark was advanced to."
    )
    acknowledged_count: int = Field(
        description=(
            "How many previously-unacknowledged alerts this call cleared from the "
            "standing queue (before minus after)."
        )
    )
    remaining: int = Field(
        description=(
            "Unacknowledged alerts still newer than the watermark after this call "
            "(non-zero only if new alerts carry a ts past up_to_ts)."
        )
    )
