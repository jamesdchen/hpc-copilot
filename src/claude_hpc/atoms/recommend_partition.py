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
from claude_hpc._schema_models.queries.recommend_partition import (
    PartitionInfo,
    RecommendPartitionResult,
    RecommendPartitionSpec,
)

if TYPE_CHECKING:
    from pathlib import Path


def _highest_priority_non_debug(parts: list[PartitionInfo]) -> PartitionInfo | None:
    """Return the highest-priority non-debug partition, or None if only debug exists.

    Returns None when ``parts`` is empty or contains only debug partitions.
    Callers must handle the None case (recommending the debug-overrun fallback
    was wrong because the debug partition would kill the very job we routed
    away from).
    """
    non_debug = [p for p in parts if not p.is_debug]
    if not non_debug:
        return None
    return max(non_debug, key=lambda p: p.priority_tier)


@primitive(
    name="recommend-partition",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-agent recommend-partition --spec <path>",
    agent_facing=True,
)
def recommend_partition(
    experiment_dir: Path,  # noqa: ARG001 — convention: every atom takes experiment_dir
    *,
    spec: RecommendPartitionSpec,
) -> RecommendPartitionResult:
    """Pick a partition for the requested walltime and return the rationale."""
    if not spec.partitions:
        return RecommendPartitionResult(
            recommended_partition="",
            rationale="no_partitions_declared",
            message=(
                "Cluster declared no partitions. Provide at least one "
                "non-empty entry in spec.partitions before requesting a "
                "recommendation."
            ),
            leverage_estimate=1.0,
        )

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
    fallback_tier = max(fallback.priority_tier, 1) if fallback is not None else 1

    if debug is None:
        # _highest_priority_non_debug returns None only when every entry is
        # debug; if debug is also None we already returned above (empty list).
        assert fallback is not None
        return RecommendPartitionResult(
            recommended_partition=fallback.name,
            rationale="no_debug_partition_available",
            message=(
                f"No debug partition declared for this cluster; routing to "
                f"highest-priority partition {fallback.name!r}."
            ),
            leverage_estimate=1.0,
        )

    # debug.walltime_cap_sec == None means the partition has no
    # walltime cap declared; treat that as "any walltime is in-bounds".
    # Coalescing None to 0 (the previous behaviour) routed every job to
    # debug_overrun_refused with a nonsensical "> 0s cap" message.
    cap = debug.walltime_cap_sec
    if cap is None or spec.requested_walltime_sec <= cap:
        # In the debug-fits case we want the debug partition itself, regardless
        # of whether a non-debug fallback exists.
        fallback_name = fallback.name if fallback is not None else debug.name
        leverage = max(debug.priority_tier / fallback_tier, 1.0)
        cap_str = f"<= {cap}s cap" if cap is not None else "no cap"
        return RecommendPartitionResult(
            recommended_partition=debug.name,
            rationale="debug_short_walltime",
            message=(
                f"Routing to {debug.name!r} (walltime {spec.requested_walltime_sec}s "
                f"{cap_str}; PriorityTier {debug.priority_tier} vs "
                f"{fallback_tier} on {fallback_name!r})."
            ),
            leverage_estimate=leverage,
        )

    if fallback is None:
        # Only debug is declared and it can't handle the walltime — refuse
        # rather than silently route back to the partition that would kill
        # the job mid-flight.
        return RecommendPartitionResult(
            recommended_partition="",
            rationale="only_debug_available_walltime_too_long",
            message=(
                f"Only a debug partition ({debug.name!r}) is declared; "
                f"requested walltime {spec.requested_walltime_sec}s exceeds "
                f"its {cap}s cap. No safe partition available — declare a "
                f"non-debug partition or shorten the walltime ask."
            ),
            leverage_estimate=1.0,
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
