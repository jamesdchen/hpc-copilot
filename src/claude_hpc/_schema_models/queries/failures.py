"""Pydantic model for the ``failures`` query atom's output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from claude_hpc._schema_models._shared import RunIdLoose, Scheduler


class _FailureCluster(BaseModel):
    model_config = ConfigDict(extra="allow")

    error_class: str | None = None
    task_ids: list[int] | None = None


class FailuresResult(BaseModel):
    """Shape of the ``data`` field on a successful ``failures`` envelope.

    Source: ``claude_hpc.atoms.failures.fetch_failures``. The
    ``preempted_count`` and ``preempted_task_ids`` fields are
    present only when at least one task was preempted by the
    scheduler — they let an agent harness branch on 'campus user got
    bumped, resubmit cleanly' without parsing per-cluster
    ``error_class`` strings.
    """

    model_config = ConfigDict(extra="forbid", title="failures output data")

    run_id: RunIdLoose
    failed_count: int = Field(
        ge=0,
        description="Total number of failed task ids in the current status report.",
    )
    clusters: list[_FailureCluster] = Field(
        description="Failure-fingerprint clusters. Each item groups failed task ids by stderr signature.",
    )
    scheduler: Scheduler | None = Field(
        default=None,
        description="Per-cluster scheduler driver, surfaced for downstream resubmit shaping.",
    )
    preempted_count: int | None = Field(
        default=None,
        ge=1,
        description="Number of task ids whose failure cluster has error_class='preempted'. Optional — present only when > 0.",
    )
    preempted_task_ids: list[int] | None = Field(
        default=None,
        description="Sorted, deduplicated list of preempted task ids. Optional — present only when at least one preempted task is in the cluster set.",
    )
    auto_retry_policy: dict[str, Any] | None = Field(
        default=None,
        description="Resolved per-run + framework-default auto-retry policy. Present when annotation succeeded.",
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic note (e.g. 'no failed tasks in current status report').",
    )
