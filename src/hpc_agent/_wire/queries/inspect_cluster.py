"""Pydantic model for the ``inspect-cluster`` query atom's output."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import Scheduler


class _ParallelEnvironment(BaseModel):
    """One scheduler 'parallel environment' — the named pool you target for work.

    Normalized across schedulers (#293) so a consumer never branches on
    scheduler_kind: SGE PEs, SLURM partitions, and PBS queues all project onto
    the SAME core. Scheduler-specific extras live in ``raw`` (whose keys DO vary)
    rather than the core, so the core never lies about uniformity — e.g. SGE's
    ``allocation_rule`` and the fuzzy per-scheduler ``slots`` (SGE/SLURM total
    capacity vs PBS per-job ceiling) are raw, not core.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="What you request: SGE -pe NAME / SLURM -p NAME / PBS -q NAME.")
    source: Literal["pe", "partition", "queue"] = Field(
        description="Which scheduler concept this is: SGE PE, SLURM partition, or PBS queue."
    )
    kind: Literal["smp", "mpi", "other"] = Field(
        description=(
            "Node-span capability: smp = single-node only, mpi = multi-node capable, "
            "other = couldn't be classified (inspect the raw allocation_rule)."
        )
    )
    max_nodes: int | None = Field(
        default=None,
        description="Max nodes a single job may span here; None = unbounded or unknown.",
    )
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Scheduler-specific detail whose keys vary by source: SGE carries "
            "{allocation_rule, slots}, SLURM/PBS carry {slots}. slots' meaning is "
            "scheduler-specific (SGE/SLURM total capacity; PBS per-job ncpus ceiling)."
        ),
    )


class _NodeSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    alloc_mem_pct: float | None = Field(default=None, ge=0, le=1)
    cpu_load_frac: float | None = Field(default=None, ge=0)
    gres: str | None = None
    gres_used: str | None = None
    active_features: list[str] | None = None
    co_tenants: list[dict[str, Any]] | None = None
    is_stressed: bool | None = None
    is_drained: bool | None = None


class InspectClusterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="inspect-cluster output data")

    cluster: str
    scheduler_kind: Scheduler
    now_iso: str = Field(description="UTC ISO-8601 timestamp of the snapshot.")
    nodes: list[_NodeSnapshot]
    errors: list[str] = Field(
        description="Non-fatal per-node errors (e.g. one node failed to report). Fatal cases raise ssh_unreachable instead.",
    )
    parallel_environments: list[_ParallelEnvironment] = Field(
        default_factory=list,
        description=(
            "Scheduler 'parallel environments' — the named pools you target for "
            "(multi-rank) work — normalized across SGE PEs (qconf -spl/-sp), SLURM "
            "partitions (scontrol show partition), and PBS execution queues "
            "(qstat -Qf) onto one shape. Empty pre-#293 or when none are exposed."
        ),
    )
