"""Pydantic model for the ``poll-run-status`` query atom's output (status)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._schema_models._shared import (
    CombinedWaves,
    FailedWaves,
    LifecycleStateObservableWithTimeout,
    RunIdLoose,
)


class StatusResult(BaseModel):
    """Shape of the ``data`` field on a successful ``status`` envelope."""

    model_config = ConfigDict(extra="forbid", title="status output data")

    run_id: RunIdLoose
    lifecycle_state: LifecycleStateObservableWithTimeout
    last_status: dict[str, Any] = Field(
        description="Snapshot from the on-cluster status reporter; see hpc_agent.models.mapreduce.reduce.status.",
    )
    last_status_age_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Age of last_status.checked_at in seconds; null when unavailable. Callers may treat any value above their threshold as stale.",
    )
    combined_waves: CombinedWaves
    failed_waves: FailedWaves
    campaign_id: str | None = Field(
        default=None,
        description="Closed-loop campaign tag. Present only when this run is part of a campaign; absent for open-loop submits.",
    )
    preempted_count: int | None = Field(
        default=None,
        ge=1,
        description="Number of task ids whose per-task sidecar entry carries a `preempt` block. Optional — present only when at least one task was preempted.",
    )
    preempted_task_ids: list[int] | None = Field(
        default=None,
        description="Sorted, deduplicated list of preempted task ids. Optional — present only when preempted_count is also present.",
    )
