"""Pydantic model for the ``stages`` spec — output shape of ``.hpc/stages.py::stages()``.

The wire schema's root is an array (not an object), so the codegen
uses Pydantic's ``TypeAdapter`` instead of a top-level BaseModel.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from hpc_agent._schema_models._shared import GpuType, Runtime


class _StageResources(BaseModel):
    model_config = ConfigDict(extra="allow")

    cpus: int | None = Field(default=None, ge=1)
    mem: str | None = Field(default=None, min_length=1)
    walltime: str | None = Field(default=None, min_length=1)
    gpus: int | None = Field(default=None, ge=0)
    gpu_type: GpuType | None = None


class _StageEnv(BaseModel):
    model_config = ConfigDict(extra="allow")

    modules: str | None = None
    conda_env: str | None = None


class StageEntry(BaseModel):
    """Each entry describes one stage in a multi-stage DAG.

    Stage names must be unique within the list. ``depends_on``
    references must resolve to other entries in the same list.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        pattern=r"^[A-Za-z][A-Za-z0-9_\-]*$",
        description="Stage identifier; unique within the DAG.",
    )
    run: str = Field(
        min_length=1,
        description="Shell command (executor invocation) for this stage.",
    )
    depends_on: str | list[str] | None = Field(
        default=None,
        description="Stage(s) that must complete before this one starts. String or list of strings.",
    )
    resources: _StageResources | None = None
    env: _StageEnv | None = None
    env_group: str | None = Field(default=None, min_length=1)
    constraints: dict[str, Any] | None = None
    results: dict[str, Any] | None = None
    runtime: Runtime | None = None
    max_retries: int | None = Field(default=None, ge=0)


# Root is an array — TypeAdapter handles list-rooted schema emission.
# ``Annotated[..., Field(min_length=1)]`` projects to ``minItems: 1`` on
# the emitted schema, matching the original hand-authored constraint.
StagesAdapter: TypeAdapter[list[StageEntry]] = TypeAdapter(
    Annotated[list[StageEntry], Field(min_length=1)]
)
