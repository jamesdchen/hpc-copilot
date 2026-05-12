"""Pydantic models for the ``submit-spec`` (legacy ``submit``) wire contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from claude_hpc._schema_models._shared import (
    CampaignId,
    RunIdLoose,
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


class SubmitResult(BaseModel):
    """Shape of the ``data`` field on a successful ``submit`` envelope."""

    model_config = ConfigDict(extra="forbid", title="submit output data")

    run_id: RunIdLoose
    job_ids: list[str] = Field(min_length=1)
    total_tasks: int = Field(ge=1)
    deduped: bool
