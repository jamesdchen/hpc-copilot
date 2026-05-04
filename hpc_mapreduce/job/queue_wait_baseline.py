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
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from hpc_mapreduce._time import parse_iso_utc_or_none, utcnow
from hpc_mapreduce.job.runtime_prior import read_samples

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
    """

    predicted_wait_sec: int | None
    confidence: Confidence
    method: Method
    n_bucket_samples: int
    n_total_samples: int
    bucket_hour_of_week: int
    fallback_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_HOURS_PER_WEEK = 168
_DEFAULT_HALF_LIFE_DAYS = 14.0
_DEFAULT_MIN_BUCKET_SAMPLES = 5
_DEFAULT_MIN_GLOBAL_SAMPLES = 20
_DEFAULT_BUCKET_RADIUS = 1


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
            return _global_fallback(
                buckets, target_bucket, n_total, "bucket weights summed to zero"
            )
        confidence: Confidence = (
            "high" if n_bucket >= 4 * min_bucket_samples else "medium"
        )
        return PredictionResult(
            predicted_wait_sec=int(round(m)),
            confidence=confidence,
            method="diurnal_ma",
            n_bucket_samples=n_bucket,
            n_total_samples=n_total,
            bucket_hour_of_week=target_bucket,
            fallback_reason=None,
        )

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
            return PredictionResult(
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

    # Tier 3: global fallback across all buckets.
    return _global_fallback(
        buckets,
        target_bucket,
        n_total,
        f"target bucket had only {n_bucket}; neighbour blend insufficient",
    )


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
