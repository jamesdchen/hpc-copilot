"""Tests for ``recommend-partition``.

Pure local primitive — exhaustive over the 5 routing rationales.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc._schema_models.queries.recommend_partition import (
    PartitionInfo,
    RecommendPartitionSpec,
)
from claude_hpc.atoms.recommend_partition import recommend_partition

if TYPE_CHECKING:
    from pathlib import Path


_DEBUG = PartitionInfo(name="debug", priority_tier=10, walltime_cap_sec=3600, is_debug=True)
_NORMAL = PartitionInfo(name="normal", priority_tier=1, walltime_cap_sec=86400)
_GPU = PartitionInfo(name="gpu", priority_tier=2, walltime_cap_sec=86400)


# ─── debug short walltime → biggest leverage ─────────────────────────


def test_short_walltime_routes_to_debug(tmp_path: Path) -> None:
    out = recommend_partition(
        tmp_path,
        spec=RecommendPartitionSpec(
            requested_walltime_sec=1800, partitions=[_DEBUG, _NORMAL, _GPU]
        ),
    )
    assert out.recommended_partition == "debug"
    assert out.rationale == "debug_short_walltime"
    assert out.leverage_estimate == 5.0  # 10 / 2 (gpu has higher tier than normal)


def test_at_debug_cap_inclusive_routes_to_debug(tmp_path: Path) -> None:
    """Walltime exactly at the cap is still acceptable on debug."""
    out = recommend_partition(
        tmp_path,
        spec=RecommendPartitionSpec(requested_walltime_sec=3600, partitions=[_DEBUG, _NORMAL]),
    )
    assert out.recommended_partition == "debug"


# ─── debug overrun → refused ──────────────────────────────────────────


def test_walltime_exceeds_debug_cap_routes_to_normal(tmp_path: Path) -> None:
    """3601s > 3600s cap → refused; falls back to highest-priority
    non-debug. Mid-flight kill is a worse outcome than slower start."""
    out = recommend_partition(
        tmp_path,
        spec=RecommendPartitionSpec(
            requested_walltime_sec=3601, partitions=[_DEBUG, _NORMAL, _GPU]
        ),
    )
    assert out.recommended_partition == "gpu"
    assert out.rationale == "debug_overrun_refused"


def test_long_walltime_routes_to_highest_priority_non_debug(tmp_path: Path) -> None:
    out = recommend_partition(
        tmp_path,
        spec=RecommendPartitionSpec(
            requested_walltime_sec=43200, partitions=[_DEBUG, _NORMAL, _GPU]
        ),
    )
    assert out.recommended_partition == "gpu"
    assert out.rationale == "debug_overrun_refused"


# ─── no debug available ───────────────────────────────────────────────


def test_no_debug_partition_falls_back_to_highest_tier(tmp_path: Path) -> None:
    out = recommend_partition(
        tmp_path,
        spec=RecommendPartitionSpec(requested_walltime_sec=900, partitions=[_NORMAL, _GPU]),
    )
    assert out.recommended_partition == "gpu"
    assert out.rationale == "no_debug_partition_available"


# ─── user preference honoured ─────────────────────────────────────────


def test_user_preference_honoured_even_when_router_disagrees(tmp_path: Path) -> None:
    """User says 'use normal' for a 30-min job; we'd have picked debug.
    Honour the user — they may know something we don't (campaign QOS,
    cluster reservation, etc.)."""
    out = recommend_partition(
        tmp_path,
        spec=RecommendPartitionSpec(
            requested_walltime_sec=1800,
            partitions=[_DEBUG, _NORMAL],
            user_preferred_partition="normal",
        ),
    )
    assert out.recommended_partition == "normal"
    assert out.rationale == "user_preference_honoured"


def test_unknown_user_preference_silently_ignored(tmp_path: Path) -> None:
    """If the user names a partition that doesn't exist, fall through
    to the smart router — the partition isn't on this cluster."""
    out = recommend_partition(
        tmp_path,
        spec=RecommendPartitionSpec(
            requested_walltime_sec=1800,
            partitions=[_DEBUG, _NORMAL],
            user_preferred_partition="ghost-partition",
        ),
    )
    assert out.recommended_partition == "debug"


# ─── debug-only cluster ───────────────────────────────────────────────


def test_only_debug_available_uses_it(tmp_path: Path) -> None:
    out = recommend_partition(
        tmp_path,
        spec=RecommendPartitionSpec(requested_walltime_sec=900, partitions=[_DEBUG]),
    )
    assert out.recommended_partition == "debug"
