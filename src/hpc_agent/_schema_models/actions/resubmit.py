"""Pydantic model for the ``resubmit-failed`` mutator's input."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._schema_models._shared import BackendName, FailureCategoryResubmittable


class ResubmitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", title="resubmit input spec")

    failed_task_ids: list[int] = Field(min_length=1)
    category: FailureCategoryResubmittable = Field(
        description=(
            "Failure category that drives retry policy. Superset of "
            "``classify_failure``'s ``CATEGORIES`` (which never emits "
            "``preempted`` directly — that's a scheduler-level state)."
        ),
    )
    overrides: dict[str, Any] | None = Field(
        default=None,
        description="Resource overrides for retry (mem, walltime, gpus, ...).",
    )
    new_job_ids: list[str] | None = None
    request_id: str | None = Field(
        default=None,
        description=(
            "Optional dedupe key. When omitted, hpc-agent derives a "
            "deterministic id from (sorted failed_task_ids, category, "
            "sorted overrides). A second resubmit with the same "
            "request_id returns the existing record without "
            "incrementing retry counters."
        ),
    )
    consult_forecast: bool = Field(
        default=True,
        description=(
            "When true (the default), hpc-agent consults the "
            "queue-wait forecaster before resubmitting and attaches a "
            "ResubmitRecommendation envelope to the response. "
            "Advisory only — does not block the resubmit. Set to "
            "false to skip the forecast call (e.g. tight CI loops)."
        ),
    )
    forecast_within_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Horizon (hours) for the resubmit-window advisor. Ignored unless consult_forecast is true.",
    )
    submit_to_cluster: bool = Field(
        default=False,
        description=(
            "When true, hpc-agent composes resubmit_plan + "
            "backend.submit_array to actually re-issue the failed "
            "tasks. When false (legacy default), the operation is "
            "journal-only. Requires script, backend, and job_name "
            "to be set when true."
        ),
    )
    script: str | None = Field(
        default=None,
        description="Path on the cluster to the submission script. Required when submit_to_cluster is true; ignored otherwise.",
    )
    backend: BackendName | None = Field(
        default=None,
        description="Scheduler backend kind. Required when submit_to_cluster is true; ignored otherwise.",
    )
    job_name: str | None = Field(
        default=None,
        description="Job name for the resubmitted batches. Required when submit_to_cluster is true; ignored otherwise.",
    )
    job_env: dict[str, str] | None = Field(
        default=None,
        description="Environment variables to forward to each resubmitted batch. Same shape submit-flow accepts. Ignored unless submit_to_cluster is true.",
    )

    @model_validator(mode="after")
    def _enforce_cluster_submit_fields(self) -> ResubmitSpec:
        if self.submit_to_cluster:
            missing = [
                name
                for name, value in (
                    ("script", self.script),
                    ("backend", self.backend),
                    ("job_name", self.job_name),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"submit_to_cluster=true requires {', '.join(missing)} to be set")
        return self
