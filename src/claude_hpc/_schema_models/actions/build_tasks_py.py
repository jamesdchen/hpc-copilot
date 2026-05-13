"""Pydantic model for the ``build-tasks-py`` scaffold's input."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _AxisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    values: list[Any] = Field(min_length=1)


class _FlagSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # Restricted to the four whitelist entries in
    # ``claude_hpc.atoms.build_tasks_py._FLAG_TYPE_NAMES``. Before v3 this
    # was an unconstrained ``str`` whose value was rendered verbatim into
    # ``.hpc/tasks.py``'s ``flag(name, <type_token>)`` call — any non-token
    # string (``"__import__('os').system('rm -rf /')"`` etc) detonated on
    # the next ``import tasks`` (v3 BUG-3V3-1, code injection at spec
    # boundary). New ctors must be added to ``_FLAG_TYPE_NAMES`` first.
    type: Literal["int", "float", "str", "bool"] = Field(
        description="One of int|float|str|bool — the four ctors the scaffold knows how to emit.",
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
