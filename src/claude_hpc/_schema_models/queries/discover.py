"""Pydantic model for the ``discover-executors`` query atom's output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ExecutorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    cli_framework: str | None = None
    has_main_guard: bool


class _DiscoverMeta(BaseModel):
    """Integrator-supplied experiment context, extracted from meta.json."""

    model_config = ConfigDict(extra="allow")

    experiment_id: str | None = None
    seed: int | float | None = None
    purpose: str | None = None
    tier: Literal[1, 2] | None = None


class DiscoverResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="discover output data")

    executors: list[_ExecutorEntry]
    meta: _DiscoverMeta | None = Field(
        default=None,
        description="Integrator-supplied experiment context, extracted from meta.json. Present only when meta.json exists at the experiment-dir root.",
    )
