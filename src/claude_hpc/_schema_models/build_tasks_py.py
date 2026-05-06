"""Pydantic model for the ``build-tasks-py`` scaffold's input."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _AxisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    values: list[Any] = Field(min_length=1)


class _FlagSpec(BaseModel):
    name: str = Field(min_length=1)
    type: str = Field(
        description="Type token: int|float|str|bool, or any string the generated `flag(...)` call accepts as the type ctor.",
    )
    default: Any | None = None


class BuildTasksPyInput(BaseModel):
    """Cartesian-product axes spec + per-executor flag declarations.

    Drives ``claude_hpc.atoms.build_tasks_py`` to scaffold
    ``<experiment>/.hpc/tasks.py``.
    """

    model_config = ConfigDict(extra="forbid", title="build-tasks-py input")

    axes: list[_AxisSpec] = Field(min_length=1)
    flags_by_executor: dict[str, list[_FlagSpec]] = Field(min_length=1)
    force: bool | None = None
