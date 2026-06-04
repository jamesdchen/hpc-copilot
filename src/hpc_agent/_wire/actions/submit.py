"""Pydantic models for the ``submit-spec`` (legacy ``submit``) wire contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    CampaignId,
    RunIdStrict,
    Runtime,
    SshTarget,
)


class SubmitSpec(BaseModel):
    """Spec passed to ``hpc-agent submit --spec <file>``."""

    model_config = ConfigDict(extra="forbid", title="submit input spec")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    ssh_target: SshTarget
    remote_path: str = Field(min_length=1)
    job_name: str = Field(min_length=1)
    run_id: RunIdStrict
    job_ids: list[str] = Field(min_length=1)
    total_tasks: int = Field(ge=1)
    runtime: Runtime | None = None
    campaign_id: CampaignId | None = None
    invalidate_on_code_change: bool = Field(
        default=False,
        description=(
            "Opt-in code-iteration safety (#207). The cmd_sha dedup key is "
            "PARAMETER identity only — an executor-body edit with unchanged "
            "swept params would otherwise dedup against (and replay) the "
            "prior run's code on a journal-wiped cross-machine resubmit. When "
            "true, the run's tasks.py drift sha is folded into the cmd_sha "
            "dedup fallback so a code-only change forces a fresh run instead "
            "of a stale replay. Default false preserves param-only dedup; a "
            "detected drift still emits a warning regardless of this flag."
        ),
    )


class SubmitResult(BaseModel):
    """Shape of the ``data`` field on a successful ``submit`` envelope."""

    model_config = ConfigDict(extra="forbid", title="submit output data")

    run_id: RunIdStrict
    job_ids: list[str] = Field(min_length=1)
    total_tasks: int = Field(ge=1)
    deduped: bool
