"""Pydantic model for the ``discover-executors`` query atom's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _ExecutorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    cli_framework: str | None = None
    has_main_guard: bool


class DiscoverResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="discover output data")

    executors: list[_ExecutorEntry]
