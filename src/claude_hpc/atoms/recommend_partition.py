"""``recommend-partition`` primitive — debug-partition routing.

Catches the lesson-4 missed opportunity: SLURM debug partitions
typically run ``PriorityTier=10`` (vs 1 for normal), giving sub-1h
jobs roughly 10× the backfill leverage. The opposite trap: anything
>cap on debug is killed, often without a clear "wrong-partition"
error.

Routing rules:

1. If ``user_preferred_partition`` is set AND it exists, honour it
   verbatim (the user might know something the rule doesn't).
2. Else if a debug partition exists AND requested_walltime ≤
   debug.walltime_cap_sec → recommend debug (priority leverage).
3. Else if a debug partition exists AND requested_walltime >
   debug.walltime_cap_sec → recommend the highest-priority non-debug
   partition (the debug-overrun case is a refusal, not a fallback).
4. Else recommend the highest-priority partition.

Pure local primitive — caller passes parsed partition config; no
SSH side effects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc._internal.primitive import primitive
from claude_hpc._schema_models.recommend_partition import (
    PartitionInfo,
    RecommendPartitionResult,
    RecommendPartitionSpec,
)

if TYPE_CHECKING:
    from pathlib import Path


def _highest_priority_non_debug(parts: list[PartitionInfo]) -> PartitionInfo:
    non_debug = [p for p in parts if not p.is_debug]
    pool = non_debug or parts  # if there's only debug, use it
    return max(pool, key=lambda p: p.priority_tier)


@primitive(
    name="recommend-partition",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-mapreduce recommend-partition --spec <path>",
    agent_facing=True,
)
def recommend_partition(
    experiment_dir: Path,  # noqa: ARG001 — convention: every atom takes experiment_dir
    *,
    spec: RecommendPartitionSpec,
) -> RecommendPartitionResult:
    """Pick a partition for the requested walltime and return the rationale."""
    by_name = {p.name: p for p in spec.partitions}

    # Rule 1: honour user preference verbatim.
    if spec.user_preferred_partition and spec.user_preferred_partition in by_name:
        chosen = by_name[spec.user_preferred_partition]
        return RecommendPartitionResult(
            recommended_partition=chosen.name,
            rationale="user_preference_honoured",
            message=(
                f"Honouring user-preferred partition {chosen.name!r}. The "
                f"smart router would have picked differently — see "
                f"requested_walltime + debug-partition rules."
            ),
            leverage_estimate=float(chosen.priority_tier),
        )

    debug = next((p for p in spec.partitions if p.is_debug), None)
    fallback = _highest_priority_non_debug(spec.partitions)
    fallback_tier = max(fallback.priority_tier, 1)

    if debug is None:
        return RecommendPartitionResult(
            recommended_partition=fallback.name,
            rationale="no_debug_partition_available",
            message=(
                f"No debug partition declared for this cluster; routing to "
                f"highest-priority partition {fallback.name!r}."
            ),
            leverage_estimate=1.0,
        )

    cap = debug.walltime_cap_sec or 0
    if cap > 0 and spec.requested_walltime_sec <= cap:
        leverage = max(debug.priority_tier / fallback_tier, 1.0)
        return RecommendPartitionResult(
            recommended_partition=debug.name,
            rationale="debug_short_walltime",
            message=(
                f"Routing to {debug.name!r} (walltime {spec.requested_walltime_sec}s "
                f"<= {cap}s cap; PriorityTier {debug.priority_tier} vs "
                f"{fallback_tier} on {fallback.name!r})."
            ),
            leverage_estimate=leverage,
        )

    return RecommendPartitionResult(
        recommended_partition=fallback.name,
        rationale="debug_overrun_refused",
        message=(
            f"Refusing to route to {debug.name!r}: requested "
            f"{spec.requested_walltime_sec}s > {cap}s cap; an overrun would "
            f"be killed mid-flight. Routing to {fallback.name!r}."
        ),
        leverage_estimate=1.0,
    )
