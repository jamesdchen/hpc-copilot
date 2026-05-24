"""Pydantic model for the ``list-in-flight`` query atom's output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdLoose


class _InFlightRun(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: RunIdLoose
    profile: str
    cluster: str
    job_ids: list[str]
    total_tasks: int = Field(ge=1)
    submitted_at: str
    campaign_id: str | None = None
    last_status: dict[str, Any] | None = None


class ListInFlightResult(BaseModel):
    """Recovery path for a fresh agent / Claude Code session.

    Discover what's still running before deciding whether to launch
    new work or resume monitoring.
    """

    model_config = ConfigDict(extra="forbid", title="list-in-flight output data")

    runs: list[_InFlightRun]
