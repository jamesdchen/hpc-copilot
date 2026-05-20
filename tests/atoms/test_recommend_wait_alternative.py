"""Tests for ``recommend-wait-alternative``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent._schema_models.queries.recommend_wait_alternative import (
    RecommendWaitAlternativeSpec,
    _PrioritySampleSpec,
)
from hpc_agent.atoms.recommend_wait_alternative import recommend_wait_alternative

if TYPE_CHECKING:
    from pathlib import Path


def _samples(*pairs: tuple[str, int]) -> list[_PrioritySampleSpec]:
    return [_PrioritySampleSpec(observed_at_iso=t, priority=p) for t, p in pairs]


# ─── basic forecast pipeline ──────────────────────────────────────────


def test_two_sample_two_point_estimate_drives_forecast(tmp_path: Path) -> None:
    """Two samples → two-point slope; forecasts populate for every
    horizon at ``current + rate * hours``."""
    out = recommend_wait_alternative(
        tmp_path,
        spec=RecommendWaitAlternativeSpec(
            current_priority=1000,
            samples=_samples(
                ("2026-04-15T03:00:00+00:00", 100),
                ("2026-04-15T04:00:00+00:00", 160),  # 60 priority/hour
            ),
            wait_horizons_hours=[1.0, 6.0],
        ),
    )
    assert out.method == "two_point"
    assert out.rate_priority_per_hour == 60.0
    forecasts = {f.wait_hours: f.forecast_priority for f in out.forecasts}
    assert forecasts == {1.0: 1060, 6.0: 1360}


def test_three_plus_samples_uses_linear_regression(tmp_path: Path) -> None:
    out = recommend_wait_alternative(
        tmp_path,
        spec=RecommendWaitAlternativeSpec(
            current_priority=500,
            samples=_samples(
                ("2026-04-15T03:00:00+00:00", 100),
                ("2026-04-15T04:00:00+00:00", 150),
                ("2026-04-15T05:00:00+00:00", 200),
            ),
            wait_horizons_hours=[2.0],
        ),
    )
    assert out.method == "linear_regression"
    assert out.n_samples == 3
    assert abs(out.rate_priority_per_hour - 50.0) < 1e-9
    assert out.forecasts[0].forecast_priority == 600


# ─── insufficient data and reset paths ────────────────────────────────


def test_insufficient_data_yields_no_forecasts(tmp_path: Path) -> None:
    """One sample → method=insufficient_data; rate=0 ⇒ no forecasts.
    The agent should surface "no data" rather than a misleading
    'wait N hours' UI."""
    out = recommend_wait_alternative(
        tmp_path,
        spec=RecommendWaitAlternativeSpec(
            current_priority=500,
            samples=_samples(("2026-04-15T03:00:00+00:00", 100)),
            wait_horizons_hours=[1.0, 6.0],
        ),
    )
    assert out.method == "insufficient_data"
    assert out.rate_priority_per_hour == 0.0
    assert out.forecasts == []


def test_no_samples_at_all_returns_insufficient_data(tmp_path: Path) -> None:
    out = recommend_wait_alternative(
        tmp_path,
        spec=RecommendWaitAlternativeSpec(current_priority=500, samples=[]),
    )
    assert out.method == "insufficient_data"
    assert out.n_samples == 0
    assert out.forecasts == []


def test_priority_reset_clamped_to_zero_with_method_tag(tmp_path: Path) -> None:
    """Observed priority went DOWN — method=reset_observed; rate
    clamped to 0; no forecasts emitted (agent shouldn't trust)."""
    out = recommend_wait_alternative(
        tmp_path,
        spec=RecommendWaitAlternativeSpec(
            current_priority=500,
            samples=_samples(
                ("2026-04-15T03:00:00+00:00", 200),
                ("2026-04-15T04:00:00+00:00", 100),
            ),
            wait_horizons_hours=[1.0, 6.0],
        ),
    )
    assert out.method == "reset_observed"
    assert out.rate_priority_per_hour == 0.0
    assert out.forecasts == []


# ─── horizon edge cases ──────────────────────────────────────────────


def test_zero_or_negative_horizons_skipped(tmp_path: Path) -> None:
    """Defensive: a 0-hour horizon ('forecast right now') has no value
    over current_priority; skip those out of the forecast list."""
    out = recommend_wait_alternative(
        tmp_path,
        spec=RecommendWaitAlternativeSpec(
            current_priority=1000,
            samples=_samples(
                ("2026-04-15T03:00:00+00:00", 100),
                ("2026-04-15T04:00:00+00:00", 160),
            ),
            wait_horizons_hours=[0.0, 1.0, -1.0, 3.0],
        ),
    )
    horizons = {f.wait_hours for f in out.forecasts}
    assert horizons == {1.0, 3.0}


def test_default_wait_horizons_used_when_unspecified(tmp_path: Path) -> None:
    """Default horizons are ``[1, 3, 6, 12, 24]``."""
    out = recommend_wait_alternative(
        tmp_path,
        spec=RecommendWaitAlternativeSpec(
            current_priority=1000,
            samples=_samples(
                ("2026-04-15T03:00:00+00:00", 100),
                ("2026-04-15T04:00:00+00:00", 160),
            ),
        ),
    )
    horizons = sorted(f.wait_hours for f in out.forecasts)
    assert horizons == [1.0, 3.0, 6.0, 12.0, 24.0]
