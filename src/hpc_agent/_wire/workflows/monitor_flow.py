"""Pydantic models for the ``monitor-flow`` workflow atom's wire contract."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    CombinedWaves,
    FailedWaves,
    LifecycleStateTerminal,
    RunIdStrict,
)


class MonitorFlowSpec(BaseModel):
    """Spec passed to ``hpc-agent monitor-flow --spec <file>``.

    Workflow atom that polls a run to terminal state, auto-combines
    waves as they finish, and writes the same .monitor.jsonl tick log
    that /monitor-hpc writes — so summary mode works regardless of
    which surface drove monitoring. MVP does NOT auto-resubmit failed
    tasks (caller decides — escalation surfaces in the envelope's
    lifecycle_state and escalation_reason).
    """

    model_config = ConfigDict(extra="forbid", title="monitor-flow input spec")

    run_id: RunIdStrict
    poll_interval_seconds: float = Field(
        default=60,
        ge=5,
        description=(
            "Seconds to sleep between status polls. Match to expected "
            "per-task runtime — 60s is fine for 10-min tasks; 300s for "
            "hour-scale tasks."
        ),
    )
    wall_clock_budget_seconds: float = Field(
        default=86400,
        ge=0,
        description=(
            "Hard cap on total monitoring time. When exceeded, "
            "monitor-flow returns with lifecycle_state='timeout' and "
            "the cluster jobs continue running. Caller may re-invoke "
            "to keep watching."
        ),
    )
    auto_combine_waves: bool = Field(
        default=True,
        description=(
            "Invoke combine-wave for each newly-complete wave during "
            "the poll loop. Disable when the caller wants to defer "
            "aggregation (e.g. campaign sweep that combines all waves "
            "once at the end)."
        ),
    )
    combiner_max_retries: int = Field(
        default=1,
        ge=0,
        description=(
            "Times to retry combine-wave with force=true after first "
            "failure. Beyond this, the wave is escalated and stays in "
            "failed_waves; monitor-flow continues watching the rest "
            "of the run."
        ),
    )
    file_glob: str = Field(
        default="*",
        description=(
            "File pattern passed to the cluster-side reporter for "
            "per-task result-file presence checks. Override to e.g. "
            "'metrics_chunk_*.json' for stricter completion detection."
        ),
    )


class MonitorFlowResult(BaseModel):
    """Shape of the ``data`` field on a successful ``monitor-flow`` envelope.

    The run may have finished cleanly, failed, been abandoned, or hit
    the wall-clock budget — in every case the envelope's ``ok`` is
    true (the *flow* completed its job of watching). Caller branches
    on ``lifecycle_state`` to decide what to do next.
    """

    model_config = ConfigDict(extra="forbid", title="monitor-flow output data")

    run_id: RunIdStrict
    lifecycle_state: LifecycleStateTerminal = Field(
        description=(
            "Terminal-or-budget state. 'complete' = every task "
            "reported complete. 'failed' = at least one failure with "
            "no running/pending tasks left (no auto-resubmit in MVP). "
            "'abandoned' = recorded job_ids are no longer known to "
            "the scheduler. 'timeout' = wall_clock_budget_seconds "
            "exceeded; cluster jobs may still be running."
        ),
    )
    last_status: dict[str, Any] = Field(
        description=(
            "Final status snapshot from the cluster-side reporter "
            "(per-task counts plus checked_at timestamp)."
        ),
    )
    combined_waves: CombinedWaves
    failed_waves: FailedWaves
    ticks: int = Field(
        ge=1,
        description="Number of poll iterations executed before terminal/timeout.",
    )
    elapsed_seconds: float = Field(
        ge=0,
        description="Wall-clock seconds from monitor-flow start to return.",
    )
    escalation_reason: str | None = Field(
        default=None,
        description=(
            "When non-null, indicates why monitor-flow stopped "
            "watching even though lifecycle is not 'complete'. "
            "Examples: 'failed_tasks_no_auto_recover_in_mvp', "
            "'combiner_failed_max_retries:wave=3', "
            "'abandoned_by_reconcile'. Null when lifecycle is "
            "'complete' or 'timeout'."
        ),
    )
