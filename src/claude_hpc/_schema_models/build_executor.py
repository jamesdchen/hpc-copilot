"""Pydantic model for the ``build-executor`` scaffold's output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BuildExecutorResult(BaseModel):
    """Shape of the ``data`` field on a successful ``build-executor --name <stem>`` envelope."""

    model_config = ConfigDict(extra="forbid", title="build-executor output data")

    path: str = Field(description="Absolute path of the new file.")
    type: Literal["plain"]
    source: str = Field(description="Absolute path of the starter template the new file was copied from.")
