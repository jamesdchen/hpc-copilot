"""Pydantic model for the ``monitor-summary`` query atom's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import LifecycleStateObservableWithTimeout


class MonitorSummaryResult(BaseModel):
    """Canonical user-facing tick summary.

    Slash-command Step 7 prints headline + body verbatim.
    lifecycle_state matches the canonical 'observable_with_timeout'
    set; the no-journal case is signaled via journal_missing rather
    than a distinct lifecycle value.
    """

    model_config = ConfigDict(extra="forbid", title="monitor-summary output")

    lifecycle_state: LifecycleStateObservableWithTimeout = Field(
        description=(
            "Lifecycle state set used when an observer also surfaces "
            "'timeout' (e.g. status / reconcile reading sidecars "
            "previously marked timeout by monitor-flow). Superset of "
            "lifecycle_state_observable."
        ),
    )
    headline: str
    body: str
    armed_hint: str | None = Field(
        description="One-line note reminding the slash command to schedule the next monitor tick; null when terminal.",
    )
    journal_missing: bool = Field(
        description=(
            "True iff the journal record could not be loaded (e.g. "
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json absent). "
            "When True, headline carries an explicit no-journal "
            "message and lifecycle_state defaults to 'abandoned' "
            "(the closest semantic match — record gone)."
        ),
    )
