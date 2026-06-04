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
        description="Reconcile flips to 'abandoned' when recorded job_ids are non-empty but none are alive on the scheduler; 'unable_to_verify' when the cluster alive-check could not run (#258).",
    )
    combined_waves: CombinedWaves
    failed_waves: FailedWaves
    last_status: dict[str, Any] = Field(
        description="Refreshed snapshot from the cluster-side reporter. Same shape as poll-run-status's last_status.",
    )
