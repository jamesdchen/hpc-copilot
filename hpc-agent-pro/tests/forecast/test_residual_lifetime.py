"""Tests for ``hpc_agent_pro.forecast.residual_lifetime.predict_residual_lifetime``."""

from __future__ import annotations

from hpc_agent_pro.forecast.residual_lifetime import (
    MIN_OBSERVATIONS_FOR_PROFILE,
    predict_residual_lifetime,
)
from hpc_agent.state.user_profiles import UserProfile


def _profile(*, n: int, ratio: float = 0.85) -> UserProfile:
    return UserProfile(
        user="alice",
        n_observations=n,
        median_actual_over_ask=ratio,
    )


class TestColdStart:
    def test_below_threshold_uses_fallback(self):
        prof = _profile(n=MIN_OBSERVATIONS_FOR_PROFILE - 1, ratio=0.5)
        # ratio on profile is ignored because n_observations < threshold.
        # ask=100, elapsed=0, fallback=0.85 → expected 85.
        assert (
            predict_residual_lifetime(
                profile=prof,
                elapsed_sec=0,
                walltime_ask_sec=100,
                fallback_ratio=0.85,
            )
            == 85
        )

    def test_above_threshold_uses_profile(self):
        prof = _profile(n=MIN_OBSERVATIONS_FOR_PROFILE + 5, ratio=0.5)
        # ask=100, elapsed=0, profile_ratio=0.5 → expected 50.
        assert (
            predict_residual_lifetime(
                profile=prof,
                elapsed_sec=0,
                walltime_ask_sec=100,
                fallback_ratio=0.85,
            )
            == 50
        )


class TestMonotonicity:
    def test_residual_decreases_with_elapsed(self):
        prof = _profile(n=20, ratio=0.85)
        ask = 1000
        prev = ask
        for elapsed in (0, 100, 500, 800, 999):
            r = predict_residual_lifetime(
                profile=prof,
                elapsed_sec=elapsed,
                walltime_ask_sec=ask,
            )
            assert r <= prev
            prev = r

    def test_residual_grows_with_walltime_ask(self):
        prof = _profile(n=20, ratio=0.9)
        elapsed = 100
        prev = 0
        for ask in (200, 1000, 5000, 10000):
            r = predict_residual_lifetime(
                profile=prof,
                elapsed_sec=elapsed,
                walltime_ask_sec=ask,
            )
            assert r >= prev
            prev = r


class TestClamping:
    def test_residual_clamped_to_remaining(self):
        prof = _profile(n=20, ratio=2.0)  # absurd ratio
        # ask=100, elapsed=50 → remaining = 50; ratio*ask=200 capped at 100.
        # expected_total = min(max(50, 200), 100) = 100; residual = 50.
        assert (
            predict_residual_lifetime(
                profile=prof,
                elapsed_sec=50,
                walltime_ask_sec=100,
            )
            == 50
        )

    def test_already_overdue_returns_zero(self):
        prof = _profile(n=20, ratio=0.85)
        assert (
            predict_residual_lifetime(
                profile=prof,
                elapsed_sec=200,
                walltime_ask_sec=100,
            )
            == 0
        )

    def test_negative_inputs_return_zero(self):
        prof = _profile(n=20, ratio=0.85)
        assert (
            predict_residual_lifetime(
                profile=prof,
                elapsed_sec=-100,
                walltime_ask_sec=-50,
            )
            == 0
        )

    def test_zero_walltime_returns_zero(self):
        prof = _profile(n=20, ratio=0.85)
        assert (
            predict_residual_lifetime(
                profile=prof,
                elapsed_sec=0,
                walltime_ask_sec=0,
            )
            == 0
        )


class TestLongTail:
    def test_user_past_their_typical_endpoint(self):
        # User finishes at 60% of ask normally, but this job is
        # already at 80% — they're in the long tail. Residual should
        # be the truly remaining time (ask - elapsed), not negative.
        prof = _profile(n=50, ratio=0.6)
        # ask=100, elapsed=80, ratio*ask=60. expected_total=max(80,60)=80,
        # capped at 100. residual = 80 - 80 = 0. Then clamped to
        # remaining = 20.
        out = predict_residual_lifetime(
            profile=prof,
            elapsed_sec=80,
            walltime_ask_sec=100,
        )
        # Predicted to finish exactly at elapsed → residual 0.
        # Caller interprets as "complete now".
        assert out == 0


class TestFallbackOverride:
    def test_explicit_fallback_used_below_threshold(self):
        prof = _profile(n=1)  # very thin
        out = predict_residual_lifetime(
            profile=prof,
            elapsed_sec=0,
            walltime_ask_sec=1000,
            fallback_ratio=0.5,
        )
        assert out == 500
