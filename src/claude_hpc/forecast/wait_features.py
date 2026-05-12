"""Feature extraction for the queue-wait residual regression.

Pure function: given a parsed squeue snapshot + the user's job
parameters + (optionally) recent history snapshots and the academic
deadline calendar, returns a dict of features the LGBM model
consumes.

Design rules:
* No I/O — caller fetches inputs.
* No NumPy / Pandas — stdlib only at this layer; the LGBM training
  script handles dataframes downstream.
* Numeric / boolean / categorical features only. Categoricals are
  passed through as strings; LightGBM consumes them natively via
  ``categorical_feature``.
* Never returns ``None`` — missing features collapse to a sentinel
  (``-1`` for ints, ``""`` for strings) so the downstream dataframe
  has uniform shape across rows.

Feature tiers (matches the discussion in
``docs/internals/queue-wait-predictor-architecture.md``):

* **Tier S** — required, ~80% of variance: hour_of_week, queue depth,
  competitor count, your-rank percentile.
* **Tier A** — high value: gpu_type_specificity, fairshare, weekend
  / business hours, recent arrival rate, median running walltime.
* **Tier C** — academic-cluster only: deadline-week features.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from claude_hpc.forecast.academic_calendar import (
    DEFAULT_DEADLINES,
    Deadline,
)
from claude_hpc.forecast.academic_calendar import (
    features_at as _deadline_features_at,
)
from claude_hpc.forecast.squeue_priority_field import QueuedJob


def _hour_of_week(dt: datetime) -> int:
    """0..167 (Monday 00:00 UTC = 0, Sunday 23:00 UTC = 167)."""
    dt = dt.astimezone(timezone.utc)
    return dt.weekday() * 24 + dt.hour


def _account_tier(user: str, fairshare_by_user: dict[str, float]) -> str:
    """Bucket users by fair-share quintile when available; fall back
    to a single bucket. Coarse but captures the per-account-tier
    competitor pattern without exploding the feature space."""
    if not fairshare_by_user:
        return "unknown"
    fs = fairshare_by_user.get(user)
    if fs is None:
        return "external"  # user not in our sshare snapshot — visiting account
    # Fair-share is in [0, 1]; quintile-bucket.
    if fs >= 0.8:
        return "fs_top"
    if fs >= 0.6:
        return "fs_high"
    if fs >= 0.4:
        return "fs_mid"
    if fs >= 0.2:
        return "fs_low"
    return "fs_bottom"


def _gpu_pool_count(constraint: str) -> int:
    """Count distinct GPU pools matched by a SLURM Features expression.

    ``gpu:a100`` → 1; ``gpu:a100|v100|l40s`` → 3 (multi-pool fits in
    more shadows, predicts shorter wait). Empty / no constraint → 0.
    """
    if not constraint:
        return 0
    # SLURM uses ``|`` for OR. Splitting on ``|`` and ``&`` gives the
    # disjunction count. Strip ``gpu:`` prefix before counting.
    cleaned = constraint.strip()
    if not cleaned:
        return 0
    return len([p for p in cleaned.replace("&", "|").split("|") if p.strip()])


def extract_features(
    *,
    now: datetime,
    queue: list[QueuedJob],
    your_priority: int,
    your_partition: str,
    your_user: str | None = None,
    your_constraint: str = "",
    partition_slot_count: int | None = None,
    fairshare_by_user: dict[str, float] | None = None,
    recent_arrival_rate_per_hour: float | None = None,
    pessimistic_floor_sec: int | None = None,
    optimistic_floor_sec: int | None = None,
    deadlines: tuple[Deadline, ...] = DEFAULT_DEADLINES,
) -> dict[str, Any]:
    """Compute the LGBM feature row for one (squeue snapshot, your job) pair.

    Caller responsibilities:

    * ``queue`` is the parsed squeue at submit time (or right now,
      for inference).
    * ``your_priority`` / ``your_partition`` describe the hypothetical
      submit; for training, these are the actual job's values.
    * ``fairshare_by_user`` comes from ``sshare -P``; when absent
      (cold cluster) the user-tier feature collapses to a single
      bucket.
    * ``recent_arrival_rate_per_hour`` is the count of pending jobs
      seen in this partition over the last ~6h divided by 6. Computed
      from saved squeue snapshots; ``None`` when no history is
      available.
    * ``pessimistic_floor_sec`` / ``optimistic_floor_sec`` are the
      outputs of ``simulate_drain`` in FIFO and backfill modes
      respectively. Together they bracket the LGBM prediction; the
      regression learns the empirical residual on top.
    """
    fs = fairshare_by_user or {}
    in_partition = [j for j in queue if j.partition == your_partition]
    pending = [j for j in in_partition if j.state == "PENDING"]
    running = [j for j in in_partition if j.state == "RUNNING"]
    pending_at_or_above = [j for j in pending if j.priority >= your_priority]

    # Tier S
    features: dict[str, Any] = {
        "hour_of_week": _hour_of_week(now),
        "queue_depth_pending": len(pending),
        "queue_depth_running": len(running),
        "pending_at_or_above_priority": len(pending_at_or_above),
        "your_priority": int(your_priority),
        "mean_priority_of_pendings_ahead": (
            sum(j.priority for j in pending_at_or_above) / len(pending_at_or_above)
            if pending_at_or_above
            else 0.0
        ),
        "your_priority_percentile": (
            (1.0 - len(pending_at_or_above) / len(pending)) if pending else 1.0
        ),
        "competitor_count_external_account": sum(
            1 for j in pending if _account_tier(j.user, fs) == "external"
        ),
        "competitor_count_fs_top": sum(1 for j in pending if _account_tier(j.user, fs) == "fs_top"),
        "competitor_count_fs_high": sum(
            1 for j in pending if _account_tier(j.user, fs) == "fs_high"
        ),
        "competitor_count_fs_mid": sum(1 for j in pending if _account_tier(j.user, fs) == "fs_mid"),
        "competitor_count_fs_low": sum(1 for j in pending if _account_tier(j.user, fs) == "fs_low"),
    }

    # Tier A
    weekday = now.astimezone(timezone.utc).weekday()
    hour_utc = now.astimezone(timezone.utc).hour
    _running_time_left_sorted = sorted(
        j.time_left_sec for j in running if j.time_left_sec is not None
    )
    features.update(
        {
            "is_weekend": weekday >= 5,
            "is_business_hours_utc": 9 <= hour_utc < 17,
            "gpu_pool_count": _gpu_pool_count(your_constraint),
            "constraint_specified": bool(your_constraint),
            # Upper-median (sorted[n // 2]) for even n; matches the
            # convention in calibration.HouseEdge. Differs from
            # numpy/statistics.median which average the two middle
            # values — for small running-job counts (typical: <50) the
            # difference is at most one job's remaining time and is
            # immaterial as a feature.
            "median_running_time_left_sec": (
                _running_time_left_sorted[len(_running_time_left_sorted) // 2]
                if _running_time_left_sorted
                else -1
            ),
            "max_running_time_left_sec": max(
                (j.time_left_sec for j in running if j.time_left_sec is not None),
                default=-1,
            ),
            "recent_arrival_rate_per_hour": (
                recent_arrival_rate_per_hour if recent_arrival_rate_per_hour is not None else -1.0
            ),
            # Your own fair-share value (separate from competitor-by-tier
            # counts). Captures "you've been quiet recently → priority
            # climbs faster than absolute lookup suggests."
            "your_fairshare_value": (
                fs.get(your_user, -1.0) if (your_user is not None and fs) else -1.0
            ),
            # Partition load: running_count / partition_slot_count.
            # Sentinel -1.0 when slot count is unknown.
            "partition_load_pct": (
                len(running) / partition_slot_count
                if partition_slot_count is not None and partition_slot_count > 0
                else -1.0
            ),
            "pessimistic_floor_sec": (
                pessimistic_floor_sec if pessimistic_floor_sec is not None else -1
            ),
            "optimistic_floor_sec": (
                optimistic_floor_sec if optimistic_floor_sec is not None else -1
            ),
            "floor_gap_sec": (
                (pessimistic_floor_sec - optimistic_floor_sec)
                if pessimistic_floor_sec is not None and optimistic_floor_sec is not None
                else -1
            ),
        }
    )

    # Tier C — academic
    features.update(_deadline_features_at(now, deadlines=deadlines))

    return features


__all__ = ["extract_features"]
