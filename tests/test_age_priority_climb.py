"""Tests for ``claude_hpc.forecast.age_priority_climb``.

Pure helper — exhaustive over the regression / two-point / reset
cases. No I/O.
"""

from __future__ import annotations

from claude_hpc.forecast.age_priority_climb import (
    PrioritySample,
    estimate_climb_rate,
    forecast_priority_after,
    hours_to_climb,
)


def _samples(*pairs: tuple[str, int]) -> list[PrioritySample]:
    return [PrioritySample(observed_at_iso=t, priority=p) for t, p in pairs]


def test_two_sample_two_point_estimate() -> None:
    """Two samples → exact two-point slope. 60 priority units in 1h
    = 60 priority/hour."""
    out = estimate_climb_rate(
        _samples(
            ("2026-04-15T03:00:00+00:00", 100),
            ("2026-04-15T04:00:00+00:00", 160),
        )
    )
    assert out.method == "two_point"
    assert out.n_samples == 2
    assert out.rate_priority_per_hour == 60.0


def test_three_plus_samples_uses_linear_regression() -> None:
    """Three samples on a perfect line → OLS recovers the slope
    exactly."""
    out = estimate_climb_rate(
        _samples(
            ("2026-04-15T03:00:00+00:00", 100),
            ("2026-04-15T04:00:00+00:00", 150),
            ("2026-04-15T05:00:00+00:00", 200),
        )
    )
    assert out.method == "linear_regression"
    assert out.n_samples == 3
    assert abs(out.rate_priority_per_hour - 50.0) < 1e-9


def test_insufficient_data_yields_zero_rate() -> None:
    out = estimate_climb_rate(_samples(("2026-04-15T03:00:00+00:00", 100)))
    assert out.method == "insufficient_data"
    assert out.rate_priority_per_hour == 0.0
    assert out.n_samples == 1


def test_negative_slope_clamped_to_zero_and_method_tagged() -> None:
    """SLURM's AgePriority is monotone non-decreasing; an observed
    decrease means the priority got reset (resubmission, reservation
    pinning, etc.). Clamp the rate but tag the method so callers
    don't trust the number."""
    out = estimate_climb_rate(
        _samples(
            ("2026-04-15T03:00:00+00:00", 200),
            ("2026-04-15T04:00:00+00:00", 100),
        )
    )
    assert out.rate_priority_per_hour == 0.0
    assert out.method == "reset_observed"


def test_unparseable_timestamp_dropped() -> None:
    """One bad timestamp doesn't kill the whole estimate."""
    out = estimate_climb_rate(
        [
            PrioritySample(observed_at_iso="not-a-date", priority=999),
            PrioritySample(observed_at_iso="2026-04-15T03:00:00+00:00", priority=100),
            PrioritySample(observed_at_iso="2026-04-15T04:00:00+00:00", priority=160),
        ]
    )
    assert out.n_samples == 2
    assert out.rate_priority_per_hour == 60.0


def test_forecast_priority_after_extrapolates() -> None:
    climb_60 = estimate_climb_rate(
        _samples(
            ("2026-04-15T03:00:00+00:00", 100),
            ("2026-04-15T04:00:00+00:00", 160),
        )
    )
    assert forecast_priority_after(current_priority=200, climb=climb_60, hours=2) == 320
    # Negative / zero hours = no change.
    assert forecast_priority_after(current_priority=200, climb=climb_60, hours=0) == 200
    assert forecast_priority_after(current_priority=200, climb=climb_60, hours=-1) == 200


def test_hours_to_climb_basic() -> None:
    climb_60 = estimate_climb_rate(
        _samples(
            ("2026-04-15T03:00:00+00:00", 100),
            ("2026-04-15T04:00:00+00:00", 160),
        )
    )
    # 60 priority/hour; need 120 → 2 hours.
    assert hours_to_climb(current_priority=100, target_priority=220, climb=climb_60) == 2.0
    # Already past target → 0.
    assert hours_to_climb(current_priority=300, target_priority=200, climb=climb_60) == 0.0


def test_hours_to_climb_with_zero_rate_returns_none() -> None:
    """Insufficient data means we can't predict — surface as None
    rather than divide-by-zero / lie."""
    no_climb = estimate_climb_rate(_samples(("2026-04-15T03:00:00+00:00", 100)))
    assert hours_to_climb(current_priority=100, target_priority=200, climb=no_climb) is None
