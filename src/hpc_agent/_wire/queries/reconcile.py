"""Pydantic model for the ``reconcile-journal`` mutator's output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    CombinedWaves,
    FailedWaves,
    LifecycleStateReconcile,
    RunIdStrict,
)


class ReconcileResult(BaseModel):
    """Shape of the ``data`` field on a successful ``reconcile`` envelope."""

    model_config = ConfigDict(extra="forbid", title="reconcile output data")

    run_id: RunIdStrict
    lifecycle_state: LifecycleStateReconcile = Field(
        description="With recorded job_ids non-empty but none alive on the scheduler, reconcile routes by the reporter's per-task evidence: 'complete' (all tasks complete, records merely purged), 'failed' (positive failed>=1 evidence — a task ran and exited non-zero; failure_features carried in last_status, #351), or 'abandoned' (no evidence on disk at all). 'unable_to_verify' when the cluster alive-check could not run (#258); 'no_run_record' for a benign crashed-submit orphan — a valid jobless sidecar with no journal record, safe to discard/overwrite (#356).",
    )
    combined_waves: CombinedWaves
    failed_waves: FailedWaves
    last_status: dict[str, Any] = Field(
        description="Refreshed snapshot from the cluster-side reporter. Same shape as poll-run-status's last_status.",
    )
