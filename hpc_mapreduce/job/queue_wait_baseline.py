"""Diurnal moving-average baseline for queue-wait forecasting.

Cluster utilization on academic HPCs is heavily seasonal: 9am Tuesday
looks nothing like 3am Saturday. The simplest forecast that beats "just
return what SLURM said" is to bucket past observations by hour-of-week
(168 buckets, ``day_of_week * 24 + hour_of_day``) and return the
bucket's exponentially-weighted mean — with sensible fallbacks when the
bucket is sparse.

Public surface:

* :func:`predict_queue_wait` — point estimate plus confidence ladder.
* :class:`PredictionResult` — frozen dataclass returned by the predictor.

Confidence ladder:

* ``high``    — target bucket has ≥ 4 × ``min_bucket_samples`` observations
* ``medium``  — target bucket has ≥ ``min_bucket_samples`` observations
* ``low``     — fell back to neighbour-blended or global mean
* ``cold``    — fewer than ``min_global_samples`` populated samples in
  the prior pool, or the supplied ``at_iso`` was unparseable. The caller
  should treat the prediction as unavailable (``predicted_wait_sec`` is
  ``None`` in this case).

Methods:

* ``"diurnal_ma"``  — target bucket alone met the threshold
* ``"blended_ma"``  — pooled the target bucket with its ±``bucket_radius``
  neighbours
* ``"global_ma"``   — pooled all buckets
* ``"no_data"``     — cold start; ``predicted_wait_sec`` is ``None``

Samples without ``submitted_at_iso`` or ``queue_wait_sec`` are silently
skipped — the prior is advisory and the older legacy samples predate the
field.

Order-book adjustment (Phase 1c)
--------------------------------
When :func:`predict_queue_wait` is called with ``current_features`` —
a :class:`~hpc_mapreduce.job.queue_features.QueueFeatures` snapshot
computed at submit time — the diurnal MA is multiplied by a bounded
factor derived from the current queue depth relative to a reference
depth. The factor is clamped to ``[_MIN_FACTOR, _MAX_FACTOR]`` so a
malformed feature payload (zero divisor, runaway queue) cannot blow
the prediction up by 100×. Confidence is **not** promoted by the
adjustment — features are advisory. The applied factor is recorded on
:attr:`PredictionResult.features_adjustment_factor` for transparency.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from hpc_mapreduce._time import parse_iso_utc_or_none, utcnow
from hpc_mapreduce.job.runtime_prior import read_samples

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_mapreduce.job.queue_features import QueueFeatures

__all__ = ["PredictionResult", "predict_queue_wait"]

Confidence = Literal["high", "medium", "low", "cold"]
Method = Literal["diurnal_ma", "blended_ma", "global_ma", "no_data"]


@dataclass(frozen=True)
class PredictionResult:
    """Outcome of a single :func:`predict_queue_wait` call.

    ``predicted_wait_sec`` is ``None`` exactly when ``method`` is
    ``"no_data"`` (cold start or unparseable ``at_iso``).

    ``bucket_hour_of_week`` is ``-1`` when the input ``at_iso`` was
    unparseable; otherwise it is the integer ``0..167`` index of the
    target bucket.

    ``features_adjustment_factor`` is the multiplicative scaling
    applied to the diurnal MA when ``current_features`` is supplied.
    1.0 means no adjustment (either features were absent, or queue
    depth matched the reference). Values >1 indicate the queue is
    currently busier than usual; <1 indicates emptier. Clamped to
    ``[_MIN_FACTOR, _MAX_FACTOR]``.
    """

    predicted_wait_sec: int | None
    confidence: Confidence
    method: Method
    n_bucket_samples: int
    n_total_samples: int
    bucket_hour_of_week: int
    fallback_reason: str | None
    features_adjustment_factor: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_HOURS_PER_WEEK = 168
_DEFAULT_HALF_LIFE_DAYS = 14.0
_DEFAULT_MIN_BUCKET_SAMPLES = 5
_DEFAULT_MIN_GLOBAL_SAMPLES = 20
_DEFAULT_BUCKET_RADIUS = 1

# Order-book adjustment caps — see module docstring.
_MIN_FACTOR = 0.5
_MAX_FACTOR = 2.0
# Strength controls how aggressively the order-book signal pushes the
# diurnal MA. 0.5 means "halve the deviation from the reference": a
# queue at 2× the reference yields factor 1.5, not 2.0. Conservative
# on purpose — the diurnal baseline is the dominant signal.
_FACTOR_STRENGTH = 0.5
# Reference depth assumed when the predictor has no historical depth
# info to compare against. Picking a non-zero default keeps the factor
# bounded when only the current snapshot is supplied; a richer
# (history-aware) reference is a future revision.
_DEFAULT_REFERENCE_DEPTH = 10


def _hour_of_week(iso: str | None) -> int | None:
    dt = parse_iso_utc_or_none(iso)
    if dt is None:
        return None
    return dt.weekday() * 24 + dt.hour


def _exp_weight(submit_iso: str, now_iso: str, half_life_days: float) -> float:
    """Exponential decay weight on sample age relative to ``now_iso``.

    Half-life parameterisation: a sample exactly ``half_life_days`` old
    contributes weight 0.5; one twice that age contributes 0.25; and so
    on. Samples timestamped *after* ``now_iso`` (e.g. simulated futures
    in tests) are clamped to weight 1.0 rather than producing weights
    > 1, which would over-amplify the freshest observations.
    """
    sd = parse_iso_utc_or_none(submit_iso)
    nd = parse_iso_utc_or_none(now_iso) or utcnow()
    if sd is None:
        return 0.0
    age_sec = (nd - sd).total_seconds()
    if age_sec < 0:
        return 1.0
    half_life_sec = half_life_days * 86400.0
    if half_life_sec <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_sec / half_life_sec)


def _wmean(observations: list[tuple[float, float]]) -> float | None:
    """Weighted mean of ``[(value, weight), ...]``. Returns None when total weight ≤ 0."""
    total_w = sum(w for _v, w in observations)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in observations) / total_w


def _features_factor(features: QueueFeatures | None) -> float:
    """Compute the multiplicative adjustment factor for *features*.

    Returns 1.0 when *features* is ``None`` or carries no usable
    queue-depth signal. The depth ratio is dampened by
    :data:`_FACTOR_STRENGTH` and clamped to ``[_MIN_FACTOR,
    _MAX_FACTOR]``. A queue 2× as deep as the reference nudges the
    prediction up by 50%, not 100%.
    """
    if features is None:
        return 1.0
    depth = features.queued_jobs_in_partition
    if depth < 0:
        return 1.0
    ref = max(1, _DEFAULT_REFERENCE_DEPTH)
    raw_ratio = depth / ref
    factor = 1.0 + (raw_ratio - 1.0) * _FACTOR_STRENGTH
    return max(_MIN_FACTOR, min(_MAX_FACTOR, factor))


def _apply_features(
    result: PredictionResult,
    features: QueueFeatures | None,
) -> PredictionResult:
    """Apply the order-book adjustment to a base :class:`PredictionResult`.

    No-op when *features* is None, when the base prediction is cold
    (``predicted_wait_sec`` is ``None``), or when the factor rounds to
    1.0 — keeps existing test contracts intact for callers that don't
    pass features.
    """
    if features is None or result.predicted_wait_sec is None:
        return result
    factor = _features_factor(features)
    if math.isclose(factor, 1.0, abs_tol=1e-9):
        return result
    adjusted = max(0, int(round(result.predicted_wait_sec * factor)))
    extra = (
        f"order-book factor={factor:.3f} "
        f"(queued_in_partition={features.queued_jobs_in_partition})"
    )
    new_reason = (
        extra if result.fallback_reason is None else f"{result.fallback_reason}; {extra}"
    )
    return PredictionResult(
        predicted_wait_sec=adjusted,
        confidence=result.confidence,
        method=result.method,
        n_bucket_samples=result.n_bucket_samples,
        n_total_samples=result.n_total_samples,
        bucket_hour_of_week=result.bucket_hour_of_week,
        fallback_reason=new_reason,
        features_adjustment_factor=round(factor, 4),
    )


def predict_queue_wait(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    at_iso: str | None = None,
    half_life_days: float = _DEFAULT_HALF_LIFE_DAYS,
    min_bucket_samples: int = _DEFAULT_MIN_BUCKET_SAMPLES,
    min_global_samples: int = _DEFAULT_MIN_GLOBAL_SAMPLES,
    bucket_radius: int = _DEFAULT_BUCKET_RADIUS,
    current_features: QueueFeatures | None = None,
) -> PredictionResult:
    """Forecast queue-wait seconds using the diurnal moving-average baseline.

    Reads the runtime-prior pool for ``(profile, cluster)`` (including
    failed runs — queue waits are observed independent of the task
    outcome), buckets populated samples by hour-of-week, and returns the
    target bucket's exponentially-weighted mean when populated. Falls
    back to neighbour-blended and then global means when the target
    bucket is sparse.

    Parameters
    ----------
    at_iso:
        Reference timestamp the forecast is *for*. ``None`` (the
        default) means "now". Passing a fixed ISO string makes the
        prediction deterministic — useful for tests and for forecasting
        a future submit window.
    half_life_days:
        Exponential decay half-life. The default (14 days) gives recent
        clusters' utilisation patterns ~4× the weight of last month's.
    min_bucket_samples:
        Minimum populated observations in a single hour-of-week bucket
        before we'll trust it on its own. Below this we widen the
        window via ``bucket_radius``.
    min_global_samples:
        Minimum populated observations across the whole prior pool
        before any forecast is produced. Below this we return cold.
    bucket_radius:
        Hours on each side of the target bucket pooled into the
        ``blended_ma`` fallback.
    current_features:
        Optional :class:`~hpc_mapreduce.job.queue_features.QueueFeatures`
        snapshot. When provided, the diurnal MA is multiplied by a
        bounded factor derived from the current queue depth (see
        :func:`_features_factor`). Confidence is preserved verbatim —
        features are advisory only and never promote a low-confidence
        forecast. The applied factor is recorded on
        :attr:`PredictionResult.features_adjustment_factor`.
    """
    now_iso = at_iso if at_iso is not None else utcnow().isoformat(timespec="seconds")
    target_bucket = _hour_of_week(now_iso)
    if target_bucket is None:
        return PredictionResult(
            predicted_wait_sec=None,
            confidence="cold",
            method="no_data",
            n_bucket_samples=0,
            n_total_samples=0,
            bucket_hour_of_week=-1,
            fallback_reason="at_iso unparseable",
        )

    # Queue-wait observations are independent of task success — a job
    # that crashed after waiting 2 hours still tells us the queue was
    # busy. Pull all samples (only_successful=False) and filter on the
    # populated fields we actually need.
    raw = read_samples(
        experiment_dir, profile=profile, cluster=cluster, only_successful=False
    )
    populated = [
        s
        for s in raw
        if s.get("submitted_at_iso") and s.get("queue_wait_sec") is not None
    ]
    n_total = len(populated)

    if n_total < min_global_samples:
        return PredictionResult(
            predicted_wait_sec=None,
            confidence="cold",
            method="no_data",
            n_bucket_samples=0,
            n_total_samples=n_total,
            bucket_hour_of_week=target_bucket,
            fallback_reason=(
                f"only {n_total} populated samples; need {min_global_samples}"
            ),
        )

    buckets: dict[int, list[tuple[float, float]]] = {}
    for s in populated:
        sub_iso = s["submitted_at_iso"]
        b = _hour_of_week(sub_iso)
        if b is None:
            continue
        w = _exp_weight(sub_iso, now_iso, half_life_days)
        if w <= 0:
            continue
        try:
            v = float(s["queue_wait_sec"])
        except (TypeError, ValueError):
            continue
        buckets.setdefault(b, []).append((v, w))

    target_obs = buckets.get(target_bucket, [])
    n_bucket = len(target_obs)

    # Tier 1: target bucket alone is dense enough.
    if n_bucket >= min_bucket_samples:
        m = _wmean(target_obs)
        if m is None:
            base = _global_fallback(
                buckets, target_bucket, n_total, "bucket weights summed to zero"
            )
            return _apply_features(base, current_features)
        confidence: Confidence = (
            "high" if n_bucket >= 4 * min_bucket_samples else "medium"
        )
        base = PredictionResult(
            predicted_wait_sec=int(round(m)),
            confidence=confidence,
            method="diurnal_ma",
            n_bucket_samples=n_bucket,
            n_total_samples=n_total,
            bucket_hour_of_week=target_bucket,
            fallback_reason=None,
        )
        return _apply_features(base, current_features)

    # Tier 2: blend with ±bucket_radius neighbours.
    blended: list[tuple[float, float]] = list(target_obs)
    for off in range(1, bucket_radius + 1):
        for d in (-off, off):
            blended.extend(
                buckets.get((target_bucket + d) % _HOURS_PER_WEEK, [])
            )
    if len(blended) >= min_bucket_samples:
        m = _wmean(blended)
        if m is not None:
            base = PredictionResult(
                predicted_wait_sec=int(round(m)),
                confidence="low",
                method="blended_ma",
                n_bucket_samples=len(blended),
                n_total_samples=n_total,
                bucket_hour_of_week=target_bucket,
                fallback_reason=(
                    f"target bucket had only {n_bucket}; "
                    f"blended +/-{bucket_radius}h"
                ),
            )
            return _apply_features(base, current_features)

    # Tier 3: global fallback across all buckets.
    base = _global_fallback(
        buckets,
        target_bucket,
        n_total,
        f"target bucket had only {n_bucket}; neighbour blend insufficient",
    )
    return _apply_features(base, current_features)


def _global_fallback(
    buckets: dict[int, list[tuple[float, float]]],
    target_bucket: int,
    n_total: int,
    reason: str,
) -> PredictionResult:
    flat: list[tuple[float, float]] = [obs for v in buckets.values() for obs in v]
    m = _wmean(flat)
    if m is None:
        return PredictionResult(
            predicted_wait_sec=None,
            confidence="cold",
            method="no_data",
            n_bucket_samples=0,
            n_total_samples=n_total,
            bucket_hour_of_week=target_bucket,
            fallback_reason=reason + "; global weights summed to zero",
        )
    return PredictionResult(
        predicted_wait_sec=int(round(m)),
        confidence="low",
        method="global_ma",
        n_bucket_samples=len(flat),
        n_total_samples=n_total,
        bucket_hour_of_week=target_bucket,
        fallback_reason=reason,
    )
