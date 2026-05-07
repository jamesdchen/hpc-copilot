"""Wire model for the ``recommend-partition`` atom.

Catches the lesson-4 missed-opportunity: SLURM debug partitions
typically have ``PriorityTier`` set 10× higher than normal but a 1h
hard cap. Sub-1h jobs get a massive backfill leverage; >1h jobs would
overrun. This atom routes short jobs to debug, refuses long ones, and
surfaces the rationale.

Pure local primitive — caller passes the cluster's known partition
config (parsed from ``clusters.yaml`` or ``scontrol show partition``);
the atom returns a recommendation + findings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PartitionInfo(BaseModel):
    """One partition the cluster exposes, with its scheduling shape."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    priority_tier: int = Field(default=1, ge=1)
    walltime_cap_sec: int | None = Field(default=None, ge=1)
    is_debug: bool = False


class RecommendPartitionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_walltime_sec: int = Field(ge=1)
    partitions: list[PartitionInfo] = Field(min_length=1)
    user_preferred_partition: str | None = None


class RecommendPartitionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_partition: str = Field(min_length=1)
    rationale: Literal[
        "user_preference_honoured",
        "debug_short_walltime",
        "debug_overrun_refused",
        "default_long_walltime",
        "no_debug_partition_available",
    ]
    message: str
    leverage_estimate: float = Field(
        default=1.0,
        ge=0.0,
        description=(
            "Multiplicative speedup the recommendation buys vs. the default "
            "partition (PriorityTier ratio). 10.0 means 10x backfill leverage."
        ),
    )
