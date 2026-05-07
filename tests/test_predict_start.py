"""Tests for ``forecast.predict_start``.

The predictor combines the FIFO + backfill simulators with an
optional LightGBM residual. These tests exercise the floor-only
path (no model) — the residual path is exercised by
``scripts/train_wait_predictor.py``'s integration test, which
requires lightgbm.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from claude_hpc.forecast.predict_start import (
    predict_start_time,
    recommend_best_submit_time,
)
from claude_hpc.forecast.squeue_priority_field import QueuedJob

if TYPE_CHECKING:
    from pathlib import Path


_NOW = "2026-09-22T10:00:00+00:00"
_NOW_DT = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)


def _q(
    job_id: str,
    priority: int,
    *,
    state: str = "PENDING",
    time_left: int | None = None,
    time_limit: int | None = None,
) -> QueuedJob:
    return QueuedJob(
        job_id=job_id,
        priority=priority,
        partition="gpu",
        user="u",
        state=state,
        time_left_sec=time_left,
        time_limit_sec=time_limit,
    )


# ─── floor-only path (no model) ──────────────────────────────────────


def test_empty_queue_predicts_now(tmp_path: Path) -> None:
    """No competitors, free slot → predicted start = now."""
    out = predict_start_time(
        tmp_path,
        now_iso=_NOW,
        queue=[],
        partition="gpu",
        partition_slot_count=1,
        your_priority=100,
        your_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    assert out.predicted_iso == _NOW.split("+")[0] + "+00:00"
    assert out.method == "floor_only"
    assert out.overhead_sec == 0


def test_pessimistic_floor_reflects_fifo_drain(tmp_path: Path) -> None:
    """One running job blocks the slot for 1h; pessimistic floor is
    +1h. With no higher-priority pendings ahead, backfill doesn't
    apply (hypo IS the front of the queue), so optimistic equals
    pessimistic in this scenario."""
    queue = [_q("r1", 999, state="RUNNING", time_left=3600)]
    out = predict_start_time(
        tmp_path,
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        your_priority=25,
        your_walltime_sec=1800,
        pending_walltime_default_sec=3600,
    )
    expected = (_NOW_DT + timedelta(hours=1)).isoformat(timespec="seconds")
    assert out.floor_pessimistic_iso == expected
    assert out.floor_optimistic_iso == expected  # hypo is front; can't backfill itself


def test_optimistic_floor_lands_hypo_in_shadow_when_blocked_by_higher_priority(
    tmp_path: Path,
) -> None:
    """The headline backfill case: a higher-priority pending blocks
    hypo; hypo's short walltime fits in the running job's shadow.
    Optimistic floor lands hypo NOW; pessimistic waits behind 'front'."""
    queue = [
        _q("r1", 999, state="RUNNING", time_left=3600),
        _q("front", 500, state="PENDING", time_limit=86400),
    ]
    out = predict_start_time(
        tmp_path,
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        your_priority=25,
        your_walltime_sec=1800,
        pending_walltime_default_sec=86400,
    )
    # Optimistic: hypo backfills NOW (1800s fits in 3600s shadow).
    assert out.floor_optimistic_iso == _NOW.split("+")[0] + "+00:00"
    # Pessimistic: hypo waits for r1 (1h) + front (24h) = 25h.
    assert out.floor_pessimistic_iso > out.floor_optimistic_iso


def test_predicted_iso_equals_pessimistic_when_no_model(tmp_path: Path) -> None:
    """Without a model, predicted_iso falls back to the pessimistic
    floor + 0 overhead."""
    queue = [_q("r1", 999, state="RUNNING", time_left=3600)]
    out = predict_start_time(
        tmp_path,
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        your_priority=25,
        your_walltime_sec=86400,
        pending_walltime_default_sec=3600,
    )
    assert out.predicted_iso == out.floor_pessimistic_iso


def test_features_are_returned_for_inspection(tmp_path: Path) -> None:
    """The forecast carries the feature dict so callers can debug
    and the agent can surface 'why this prediction'."""
    out = predict_start_time(
        tmp_path,
        now_iso=_NOW,
        queue=[],
        partition="gpu",
        partition_slot_count=1,
        your_priority=100,
        your_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    assert "hour_of_week" in out.features
    assert "pessimistic_floor_sec" in out.features
    assert "optimistic_floor_sec" in out.features


def test_pending_TimeLimit_used_for_walltime_default(tmp_path: Path) -> None:
    """When a competing pending job exposes its TimeLimit via squeue,
    the simulator uses that instead of the partition default."""
    queue = [
        _q("rival", 200, state="PENDING", time_limit=600),  # higher prio, 10min
    ]
    out = predict_start_time(
        tmp_path,
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        your_priority=100,
        your_walltime_sec=3600,
        pending_walltime_default_sec=86400,  # would predict 24h+ without TimeLimit
    )
    expected = (_NOW_DT + timedelta(minutes=10)).isoformat(timespec="seconds")
    assert out.floor_pessimistic_iso == expected


# ─── recommend_best_submit_time ──────────────────────────────────────


def test_recommend_picks_now_when_no_contention(tmp_path: Path) -> None:
    """Empty queue → submit-now is the lowest-total-time candidate."""
    out = recommend_best_submit_time(
        tmp_path,
        now_iso=_NOW,
        queue=[],
        partition="gpu",
        partition_slot_count=1,
        your_priority=100,
        your_walltime_sec=3600,
        pending_walltime_default_sec=3600,
        candidate_offsets_hours=(0, 1, 6),
    )
    assert out.best_submit_offset_hours == 0
    assert out.best_total_time_sec == 0


def test_recommend_returns_all_candidates_for_transparency(tmp_path: Path) -> None:
    """Caller can surface the full grid, not just the winner."""
    out = recommend_best_submit_time(
        tmp_path,
        now_iso=_NOW,
        queue=[],
        partition="gpu",
        partition_slot_count=1,
        your_priority=100,
        your_walltime_sec=3600,
        pending_walltime_default_sec=3600,
        candidate_offsets_hours=(0, 1, 3, 6),
    )
    assert len(out.candidates) == 4
    assert tuple(c.offset_hours for c in out.candidates) == (0, 1, 3, 6)


def test_negative_offset_filtered_out(tmp_path: Path) -> None:
    """A 'submit in the past' offset is nonsense; quietly skip it."""
    out = recommend_best_submit_time(
        tmp_path,
        now_iso=_NOW,
        queue=[],
        partition="gpu",
        partition_slot_count=1,
        your_priority=100,
        your_walltime_sec=3600,
        pending_walltime_default_sec=3600,
        candidate_offsets_hours=(-1.0, 0, 1),
    )
    offsets = {c.offset_hours for c in out.candidates}
    assert offsets == {0, 1}
