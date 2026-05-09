"""Combined floor + residual queue-wait predictor.

Architecture (see the design discussion in the session that produced
this file):

* **Pessimistic floor** — :func:`simulate_drain` in pure FIFO mode.
  Hard lower bound: "your job cannot start before this under the
  most optimistic assumption that no one else submits anything."
* **Optimistic floor** — :func:`simulate_drain` in backfill mode
  (phantom-slot approximation). Loose upper bound on how early
  backfill could plausibly let your job land.
* **LGBM residual** — gradient-boosted regression learns the empirical
  overhead between the FIFO floor and the actual observed start time.
  Captures stochastic reality: future arrivals, fair-share decay,
  reservation surprises, scheduler config drift.
* **Final prediction** — pessimistic_floor + LGBM_overhead. Both
  floors are also passed as features so the regression can use them
  empirically (the floor *gap* is a strong predictor of cluster
  slack).

The LGBM model is optional: when no model is available (cold start,
forecasting optional dep not installed), the predictor falls back to
returning the pessimistic floor + a zero overhead. The two floors
are still computed and surfaced for diagnostic purposes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from claude_hpc.forecast.academic_calendar import (
    Deadline,
    load_project_deadlines,
)
from claude_hpc.forecast.drain_simulator import simulate_drain
from claude_hpc.forecast.squeue_priority_field import QueuedJob
from claude_hpc.forecast.wait_features import extract_features

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartForecast:
    """Output of :func:`predict_start_time`.

    * ``floor_pessimistic_iso`` — pure FIFO drain prediction.
    * ``floor_optimistic_iso`` — phantom-slot backfill prediction.
    * ``predicted_iso`` — pessimistic_floor + LGBM_overhead. When the
      model is unavailable, equals ``floor_pessimistic_iso``.
    * ``overhead_sec`` — the LGBM's residual prediction; 0 when no
      model.
    * ``method`` — ``"floor_plus_residual"`` (model present),
      ``"floor_only"`` (no model), or ``"floor_only_cold_start"``
      (model present but features missing).
    """

    floor_pessimistic_iso: str
    floor_optimistic_iso: str
    predicted_iso: str
    overhead_sec: int
    method: str
    # Optional quantile predictions when the trainer fits separate
    # ``model_p10.txt`` / ``model_p90.txt`` files. ``None`` means
    # the model directory has only the median model.
    predicted_iso_p10: str | None = None
    predicted_iso_p90: str | None = None
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateForecast:
    """One (offset, forecast, total_time) tuple in the sweep."""

    offset_hours: float
    forecast: StartForecast
    total_time_sec: int


@dataclass(frozen=True)
class BestSubmitForecast:
    """Output of :func:`recommend_best_submit_time`.

    The recommender sweeps several candidate submit-at-T values and
    picks the one minimizing total time-to-actual-start (offset until
    submit + predicted wait after submission). Lets the agent surface
    "wait 6h, then submit; total time-to-start is 45min" vs. "submit
    now; total time-to-start is 4h."
    """

    best_submit_offset_hours: float
    best_predicted_start_iso: str
    best_total_time_sec: int
    candidates: tuple[CandidateForecast, ...]


def _to_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _add_hours(iso: str, hours: float) -> str:
    return (_to_dt(iso) + timedelta(hours=hours)).isoformat(timespec="seconds")


def predict_start_time(
    experiment_dir: Path,
    *,
    now_iso: str,
    queue: list[QueuedJob],
    partition: str,
    partition_slot_count: int,
    your_priority: int,
    your_walltime_sec: int,
    pending_walltime_default_sec: int,
    your_user: str | None = None,
    your_constraint: str = "",
    partition_slot_count_for_features: int | None = None,
    fairshare_by_user: dict[str, float] | None = None,
    recent_arrival_rate_per_hour: float | None = None,
    deadlines: tuple[Deadline, ...] | None = None,
    model_path: Path | None = None,
) -> StartForecast:
    """Predict the start time of a hypothetical job submitted *at_iso*.

    *model_path* points to a serialized LightGBM model produced by
    ``scripts/train_wait_predictor.py``. When absent the predictor
    returns the pessimistic floor as the predicted start (no
    residual). The two floors are always computed regardless.
    """
    deadlines = deadlines if deadlines is not None else load_project_deadlines(experiment_dir)
    pessimistic = simulate_drain(
        now_iso=now_iso,
        queue=queue,
        partition=partition,
        partition_slot_count=partition_slot_count,
        hypothetical_priority=your_priority,
        hypothetical_walltime_sec=your_walltime_sec,
        pending_walltime_default_sec=pending_walltime_default_sec,
        enable_backfill=False,
    )
    optimistic = simulate_drain(
        now_iso=now_iso,
        queue=queue,
        partition=partition,
        partition_slot_count=partition_slot_count,
        hypothetical_priority=your_priority,
        hypothetical_walltime_sec=your_walltime_sec,
        pending_walltime_default_sec=pending_walltime_default_sec,
        enable_backfill=True,
    )
    floor_pess = pessimistic.hypothetical_starts_at_iso or now_iso
    floor_opt = optimistic.hypothetical_starts_at_iso or floor_pess

    pess_dt = _to_dt(floor_pess)
    opt_dt = _to_dt(floor_opt)

    features = extract_features(
        now=_to_dt(now_iso),
        queue=queue,
        your_priority=your_priority,
        your_partition=partition,
        your_user=your_user,
        your_constraint=your_constraint,
        partition_slot_count=partition_slot_count_for_features
        if partition_slot_count_for_features is not None
        else partition_slot_count,
        fairshare_by_user=fairshare_by_user,
        recent_arrival_rate_per_hour=recent_arrival_rate_per_hour,
        pessimistic_floor_sec=int((pess_dt - _to_dt(now_iso)).total_seconds()),
        optimistic_floor_sec=int((opt_dt - _to_dt(now_iso)).total_seconds()),
        deadlines=deadlines,
    )

    overhead_sec = 0
    overhead_sec_p10: int | None = None
    overhead_sec_p90: int | None = None
    method = "floor_only"
    # ``model_path`` is either a single regression model file or a
    # directory containing ``model.txt`` (median) plus optional
    # ``model_p10.txt`` / ``model_p90.txt`` for quantile predictions.
    p50_path, p10_path, p90_path = _resolve_model_paths(model_path)
    if p50_path is not None and p50_path.is_file():
        try:
            import lightgbm as lgb  # noqa: PLC0415 — optional dep

            overhead_sec = _predict_overhead(p50_path, features, lgb)
            method = "floor_plus_residual"
            if p10_path is not None and p10_path.is_file():
                overhead_sec_p10 = _predict_overhead(p10_path, features, lgb)
            if p90_path is not None and p90_path.is_file():
                overhead_sec_p90 = _predict_overhead(p90_path, features, lgb)
        except (ImportError, ValueError, OSError):
            method = "floor_only_cold_start"

    predicted_dt = pess_dt + timedelta(seconds=overhead_sec)
    # Ensure prediction never goes backward in time (defensive).
    if predicted_dt < _to_dt(now_iso).astimezone(timezone.utc):
        predicted_dt = _to_dt(now_iso).astimezone(timezone.utc)

    predicted_iso_p10 = (
        (pess_dt + timedelta(seconds=overhead_sec_p10)).isoformat(timespec="seconds")
        if overhead_sec_p10 is not None
        else None
    )
    predicted_iso_p90 = (
        (pess_dt + timedelta(seconds=overhead_sec_p90)).isoformat(timespec="seconds")
        if overhead_sec_p90 is not None
        else None
    )

    return StartForecast(
        floor_pessimistic_iso=floor_pess,
        floor_optimistic_iso=floor_opt,
        predicted_iso=predicted_dt.isoformat(timespec="seconds"),
        predicted_iso_p10=predicted_iso_p10,
        predicted_iso_p90=predicted_iso_p90,
        overhead_sec=overhead_sec,
        method=method,
        features=features,
    )


def _resolve_model_paths(
    model_path: Path | None,
) -> tuple[Path | None, Path | None, Path | None]:
    """Accept either a single ``.txt`` file (regression model) or a
    directory containing ``model.txt`` / ``model_p10.txt`` /
    ``model_p90.txt``. Returns ``(p50, p10, p90)`` paths."""
    if model_path is None:
        return (None, None, None)
    if model_path.is_file():
        return (model_path, None, None)
    if model_path.is_dir():
        return (
            model_path / "model.txt",
            model_path / "model_p10.txt",
            model_path / "model_p90.txt",
        )
    return (None, None, None)


def _predict_overhead(model_file: Path, features: dict[str, Any], lgb: Any) -> int:
    """Run one LightGBM model on the feature row; clamp to ≥0."""
    booster = lgb.Booster(model_file=str(model_file))
    row = [_coerce_numeric(name, features.get(name, -1)) for name in booster.feature_name()]
    return max(0, int(round(float(booster.predict([row])[0]))))


def _coerce_numeric(name: str, value: Any) -> float:
    """LightGBM expects floats; coerce booleans / None / strings safely.

    None and non-numeric values fall back to the ``-1.0`` sentinel — the
    same convention :mod:`wait_features` uses for missing numerics — but
    only None and bool are silent. Any other unexpected type triggers a
    warning so silent data-quality issues surface in logs instead of
    being papered over by the sentinel.
    """
    if value is None:
        return -1.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    _log.warning(
        "feature %r has non-numeric value %r (type=%s); using -1.0 sentinel",
        name,
        value,
        type(value).__name__,
    )
    return -1.0


def recommend_best_submit_time(
    experiment_dir: Path,
    *,
    now_iso: str,
    queue: list[QueuedJob],
    partition: str,
    partition_slot_count: int,
    your_priority: int,
    your_walltime_sec: int,
    pending_walltime_default_sec: int,
    candidate_offsets_hours: tuple[float, ...] = (0, 1, 3, 6, 12, 24),
    **kwargs: Any,
) -> BestSubmitForecast:
    """For each offset, predict total time-to-actual-start; return the
    offset with the lowest total. Lets the agent surface 'wait N
    hours, then submit; total time-to-start = X min'.

    The model + queue are evaluated at each candidate's submit time;
    in our simplified model the queue at ``now + offset`` is taken to
    be the same as the queue at ``now`` (we don't simulate other
    users' future submissions — that's exactly what the regression's
    overhead term captures empirically).
    """
    candidates: list[CandidateForecast] = []
    for offset in candidate_offsets_hours:
        if offset < 0:
            continue
        submit_at = _add_hours(now_iso, offset)
        f = predict_start_time(
            experiment_dir,
            now_iso=submit_at,
            queue=queue,
            partition=partition,
            partition_slot_count=partition_slot_count,
            your_priority=your_priority,
            your_walltime_sec=your_walltime_sec,
            pending_walltime_default_sec=pending_walltime_default_sec,
            **kwargs,
        )
        total_sec = int((_to_dt(f.predicted_iso) - _to_dt(now_iso)).total_seconds())
        candidates.append(
            CandidateForecast(offset_hours=offset, forecast=f, total_time_sec=total_sec)
        )
    if not candidates:
        raise ValueError("no candidate offsets supplied")
    best = min(candidates, key=lambda c: c.total_time_sec)
    return BestSubmitForecast(
        best_submit_offset_hours=best.offset_hours,
        best_predicted_start_iso=best.forecast.predicted_iso,
        best_total_time_sec=best.total_time_sec,
        candidates=tuple(candidates),
    )


__all__ = [
    "BestSubmitForecast",
    "CandidateForecast",
    "StartForecast",
    "predict_start_time",
    "recommend_best_submit_time",
]
