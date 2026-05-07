"""Age-priority climb modelling (lesson 5).

SLURM increases pending jobs' priority over time via the AgePriority
weight. Lesson 5 from the backfill session: row 47 climbed rank
168 → 137 in 3 hours. The framework's recommender currently surfaces
"submit at hour H" via the diurnal MA but never says "doing nothing
for N hours climbs you K ranks." This module supplies the climb
estimate so the recommender can surface both alternatives and pick
the lower expected wait.

Pure stdlib. Inputs are a list of ``(observed_at_iso, priority)``
samples drawn from past pendings on the same partition; outputs are
a fitted climb rate (priority units per hour) and a forecast helper.

Default bias: WAIT. Per the conversation, the recommender's default
expectation is that doing nothing wins — your queue position
shrinks faster than any hour-H you'd find. Hour-H only beats waiting
when there's a real diurnal shadow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Literal

ClimbMethod = Literal[
    "linear_regression",
    "two_point",
    "insufficient_data",
    "reset_observed",
]


@dataclass(frozen=True)
class PrioritySample:
    """One observation: ``priority`` was the job's value at ``observed_at_iso``."""

    observed_at_iso: str
    priority: int


@dataclass(frozen=True)
class ClimbEstimate:
    """Fitted climb rate + provenance for the diagnosis."""

    rate_priority_per_hour: float
    n_samples: int
    method: ClimbMethod


def _to_dt(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def estimate_climb_rate(samples: list[PrioritySample]) -> ClimbEstimate:
    """Fit a climb rate (priority units per hour) from observations.

    Uses ordinary least-squares slope when ``len(samples) >= 3``,
    falls back to a two-point estimate when ``len(samples) == 2``,
    and emits ``method="insufficient_data"`` with rate=0 when fewer.
    The rate is ALWAYS non-negative — SLURM's AgePriority is monotone
    non-decreasing — so an estimated negative slope (caused by
    transient priority resets) gets clamped to 0 and the method is
    tagged ``method="reset_observed"`` so callers know not to trust
    the numbers.
    """
    timed = [(t, s.priority) for s in samples if (t := _to_dt(s.observed_at_iso)) is not None]
    timed.sort(key=lambda t: t[0])
    if len(timed) < 2:
        return ClimbEstimate(
            rate_priority_per_hour=0.0,
            n_samples=len(timed),
            method="insufficient_data",
        )

    if len(timed) == 2:
        (t0, p0), (t1, p1) = timed
        hours = max((t1 - t0).total_seconds() / 3600.0, 1e-6)
        slope = (p1 - p0) / hours
        return ClimbEstimate(
            rate_priority_per_hour=max(slope, 0.0),
            n_samples=2,
            method="reset_observed" if slope < 0 else "two_point",
        )

    # OLS slope; ``base_t`` recasts time to hours so the slope is in
    # the right units without depending on a specific zero point.
    base_t = timed[0][0]
    xs = [(t - base_t).total_seconds() / 3600.0 for t, _ in timed]
    ys = [p for _, p in timed]
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    den = sum((x - mx) ** 2 for x in xs)
    slope = num / den if den > 0 else 0.0
    return ClimbEstimate(
        rate_priority_per_hour=max(slope, 0.0),
        n_samples=len(timed),
        method="reset_observed" if slope < 0 else "linear_regression",
    )


def forecast_priority_after(
    *,
    current_priority: int,
    climb: ClimbEstimate,
    hours: float,
) -> int:
    """Forecast priority *hours* into the future given a fitted climb rate.

    Useful as the "do-nothing" baseline the recommender compares
    against any "submit at hour H" alternative."""
    if hours <= 0:
        return current_priority
    return int(round(current_priority + climb.rate_priority_per_hour * hours))


def hours_to_climb(
    *,
    current_priority: int,
    target_priority: int,
    climb: ClimbEstimate,
) -> float | None:
    """How long (in hours) the job needs to wait to reach
    *target_priority* at the fitted rate.

    Returns None when the climb rate is 0 (or insufficient data) —
    the caller can't predict.
    """
    if climb.rate_priority_per_hour <= 0:
        return None
    delta = target_priority - current_priority
    if delta <= 0:
        return 0.0
    return delta / climb.rate_priority_per_hour


__all__ = [
    "ClimbEstimate",
    "ClimbMethod",
    "PrioritySample",
    "estimate_climb_rate",
    "forecast_priority_after",
    "hours_to_climb",
]
