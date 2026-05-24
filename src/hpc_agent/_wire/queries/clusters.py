"""Pydantic models for ``clusters list`` / ``clusters describe`` outputs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import Scheduler


class _ClusterListEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    unknown_keys: list[str] | None = Field(
        default=None,
        description=(
            "Yaml keys present in this cluster's entry that ClusterConfig "
            "does not recognize. Populated only when --strict is passed. "
            "Empty list = clean entry; null = strict mode was not requested."
        ),
    )
