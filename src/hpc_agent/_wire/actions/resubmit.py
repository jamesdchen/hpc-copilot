"""Pydantic model for the ``resubmit-failed`` mutator's input."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import BackendName, FailureCategoryResubmittable


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
    from_checkpoint: bool = Field(
        default=False,
        description=(
            "When true, resubmitted tasks RESUME from their latest checkpoint "
            "instead of restarting (#294 PR3). Stamps HPC_RESUME_FROM_CHECKPOINT=1 "
            "into the batch job_env; the cluster-side dispatcher then locates the "
            "latest <result_dir>/_checkpoints/checkpoint-*.pkl per task and exposes "
            "it to the executor as args.resume_from / args.checkpoint_dir (env: "
            "HPC_RESUME_FROM / HPC_CHECKPOINT_DIR). No-op unless submit_to_cluster "
            "is true and the executor opts into checkpointing; a task with no "
            "checkpoint simply starts fresh."
        ),
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
