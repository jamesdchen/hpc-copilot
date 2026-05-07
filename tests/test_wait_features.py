"""Tests for ``forecast.wait_features.extract_features``.

Pure function — exhaustive over the feature classes (Tier S, A, C).
No I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone

from claude_hpc.forecast.squeue_priority_field import QueuedJob
from claude_hpc.forecast.wait_features import extract_features


def _q(
    job_id: str,
    priority: int,
    *,
    user: str = "u",
    state: str = "PENDING",
    partition: str = "gpu",
    time_left: int | None = None,
    time_limit: int | None = None,
) -> QueuedJob:
    return QueuedJob(
        job_id=job_id,
        priority=priority,
        partition=partition,
        user=user,
        state=state,
        time_left_sec=time_left,
        time_limit_sec=time_limit,
    )


_NOW = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)  # Tuesday


# ─── Tier S — required features ───────────────────────────────────────


def test_queue_depth_basic_counts() -> None:
    queue = [
        _q("a", 200, state="PENDING"),
        _q("b", 50, state="PENDING"),
        _q("c", 999, state="RUNNING", time_left=3600),
    ]
    f = extract_features(now=_NOW, queue=queue, your_priority=100, your_partition="gpu")
    assert f["queue_depth_pending"] == 2
    assert f["queue_depth_running"] == 1


def test_pending_at_or_above_priority_counted() -> None:
    queue = [
        _q("hi", 200),
        _q("eq", 100),  # equal counts as at-or-above
        _q("lo", 50),
    ]
    f = extract_features(now=_NOW, queue=queue, your_priority=100, your_partition="gpu")
    assert f["pending_at_or_above_priority"] == 2  # hi + eq


def test_priority_percentile_when_top_of_queue() -> None:
    """No competitors at-or-above priority → percentile = 1.0 (best)."""
    queue = [_q("a", 50), _q("b", 30)]
    f = extract_features(now=_NOW, queue=queue, your_priority=999, your_partition="gpu")
    assert f["your_priority_percentile"] == 1.0


def test_priority_percentile_when_bottom_of_queue() -> None:
    """Everyone is at-or-above → percentile = 0.0 (worst)."""
    queue = [_q("a", 200), _q("b", 200)]
    f = extract_features(now=_NOW, queue=queue, your_priority=100, your_partition="gpu")
    assert f["your_priority_percentile"] == 0.0


def test_competitor_count_by_account_tier_groups_by_fairshare() -> None:
    queue = [
        _q("j1", 100, user="alice"),
        _q("j2", 100, user="bob"),
        _q("j3", 100, user="carol"),
    ]
    fairshare = {"alice": 0.9, "bob": 0.5, "carol": 0.1}
    f = extract_features(
        now=_NOW,
        queue=queue,
        your_priority=50,
        your_partition="gpu",
        fairshare_by_user=fairshare,
    )
    assert f["competitor_count_fs_top"] == 1  # alice
    assert f["competitor_count_fs_mid"] == 1  # bob
    assert f["competitor_count_fs_low"] == 0  # carol is fs_bottom (<0.2)


def test_external_account_when_user_not_in_fairshare_table() -> None:
    """A user not in our sshare snapshot is a visiting/external
    account — bucket separately."""
    queue = [_q("j1", 100, user="visitor")]
    f = extract_features(
        now=_NOW,
        queue=queue,
        your_priority=50,
        your_partition="gpu",
        fairshare_by_user={"alice": 0.9},
    )
    assert f["competitor_count_external_account"] == 1


def test_partition_isolation() -> None:
    """Jobs on other partitions don't count toward your features."""
    queue = [
        _q("here", 100, partition="gpu"),
        _q("away1", 100, partition="cpu"),
        _q("away2", 100, partition="cpu"),
    ]
    f = extract_features(now=_NOW, queue=queue, your_priority=50, your_partition="gpu")
    assert f["queue_depth_pending"] == 1


# ─── Tier A — high value features ─────────────────────────────────────


def test_gpu_pool_count_for_multi_pool_constraint() -> None:
    f = extract_features(
        now=_NOW,
        queue=[],
        your_priority=100,
        your_partition="gpu",
        your_constraint="gpu:a100|v100|l40s",
    )
    assert f["gpu_pool_count"] == 3


def test_gpu_pool_count_for_single_pool() -> None:
    f = extract_features(
        now=_NOW,
        queue=[],
        your_priority=100,
        your_partition="gpu",
        your_constraint="gpu:a100",
    )
    assert f["gpu_pool_count"] == 1
    assert f["constraint_specified"] is True


def test_gpu_pool_count_zero_when_no_constraint() -> None:
    f = extract_features(now=_NOW, queue=[], your_priority=100, your_partition="gpu")
    assert f["gpu_pool_count"] == 0
    assert f["constraint_specified"] is False


def test_is_weekend_for_saturday() -> None:
    sat = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    f = extract_features(now=sat, queue=[], your_priority=100, your_partition="gpu")
    assert f["is_weekend"] is True


def test_is_business_hours_utc_midday() -> None:
    f = extract_features(now=_NOW, queue=[], your_priority=100, your_partition="gpu")
    assert f["is_business_hours_utc"] is True


def test_median_running_time_left_picks_middle_value() -> None:
    queue = [
        _q("r1", 999, state="RUNNING", time_left=600),
        _q("r2", 999, state="RUNNING", time_left=3600),
        _q("r3", 999, state="RUNNING", time_left=7200),
    ]
    f = extract_features(now=_NOW, queue=queue, your_priority=100, your_partition="gpu")
    assert f["median_running_time_left_sec"] == 3600


def test_median_running_time_left_handles_missing() -> None:
    """When no running jobs have time_left, surface -1 (sentinel)."""
    queue = [_q("r1", 999, state="RUNNING", time_left=None)]
    f = extract_features(now=_NOW, queue=queue, your_priority=100, your_partition="gpu")
    assert f["median_running_time_left_sec"] == -1


def test_floor_features_default_to_minus_one_when_unspecified() -> None:
    f = extract_features(now=_NOW, queue=[], your_priority=100, your_partition="gpu")
    assert f["pessimistic_floor_sec"] == -1
    assert f["optimistic_floor_sec"] == -1
    assert f["floor_gap_sec"] == -1


def test_floor_gap_computed_when_both_supplied() -> None:
    f = extract_features(
        now=_NOW,
        queue=[],
        your_priority=100,
        your_partition="gpu",
        pessimistic_floor_sec=3600,
        optimistic_floor_sec=600,
    )
    assert f["floor_gap_sec"] == 3000


# ─── Tier C — academic features ───────────────────────────────────────


def test_academic_calendar_features_present() -> None:
    f = extract_features(now=_NOW, queue=[], your_priority=100, your_partition="gpu")
    assert "min_days_to_deadline" in f
    assert "deadline_density_30d" in f


# ─── hour_of_week ────────────────────────────────────────────────────


def test_hour_of_week_for_known_datetime() -> None:
    """Tuesday 10:00 UTC = weekday 1 × 24 + 10 = 34."""
    f = extract_features(now=_NOW, queue=[], your_priority=100, your_partition="gpu")
    assert f["hour_of_week"] == 34


def test_hour_of_week_for_monday_midnight() -> None:
    """Monday 00:00 UTC = 0."""
    mon = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)
    f = extract_features(now=mon, queue=[], your_priority=100, your_partition="gpu")
    assert f["hour_of_week"] == 0


def test_hour_of_week_for_sunday_last_hour() -> None:
    """Sunday 23:00 UTC = 6 × 24 + 23 = 167."""
    sun = datetime(2026, 9, 27, 23, 0, 0, tzinfo=timezone.utc)
    f = extract_features(now=sun, queue=[], your_priority=100, your_partition="gpu")
    assert f["hour_of_week"] == 167
