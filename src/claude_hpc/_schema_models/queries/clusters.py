"""Pydantic models for ``clusters list`` / ``clusters describe`` outputs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from claude_hpc._schema_models._shared import Scheduler


class _ClusterListEntry(BaseModel):
    name: str
    host: str
    scheduler: Scheduler


class ClustersListResult(BaseModel):
    """Shape of the ``data`` field on a successful ``clusters list`` envelope."""

    model_config = ConfigDict(extra="forbid", title="clusters list output data")

    clusters: list[_ClusterListEntry]


class ClustersDescribeResult(BaseModel):
    """Shape of the ``data`` field on a successful ``clusters describe <name>`` envelope."""

    model_config = ConfigDict(extra="forbid", title="clusters describe output data")

    name: str
    config: dict[str, Any] = Field(
        description=(
            "Full cluster config block from clusters.yaml (host, "
            "scheduler, scratch, gpu_types, constraints, etc.). Shape "
            "varies per cluster; see config-precedence.md for fields "
            "the framework recognizes."
        ),
    )
