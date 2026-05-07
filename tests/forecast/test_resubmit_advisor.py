"""Tests for ``claude_hpc.forecast.resubmit_advisor.recommend_resubmit_window``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_hpc.forecast import best_submit_window as bsw
from claude_hpc.forecast import queue_wait_baseline as qwb
from claude_hpc.forecast.resubmit_advisor import recommend_resubmit_window
from tests.conftest import seed_diurnal_dip

PROFILE = "ml_ridge"
CLUSTER = "discovery"


def _seed_with_dip(tmp_path):
    seed_diurnal_dip(tmp_path, profile=PROFILE, cluster=CLUSTER)


def _pin_now(monkeypatch, dt: datetime) -> None:
    """Pin ``utcnow`` in both modules the advisor calls."""
    monkeypatch.setattr(bsw, "utcnow", lambda: dt)
    monkeypatch.setattr(qwb, "utcnow", lambda: dt)


@pytest.mark.slow
class TestRecommendation:
    def test_busy_hour_recommends_wait_when_dip_is_within_horizon(self, tmp_path, monkeypatch):
        _seed_with_dip(tmp_path)
        # Pin now to Mon 14:00 UTC — a busy bucket. Dip at 03-06 sits
        # inside the next 24h.
        _pin_now(monkeypatch, datetime(2026, 4, 15, 14, 0, 0, tzinfo=timezone.utc))

        rec = recommend_resubmit_window(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            within_hours=24,
        )
        assert rec.recommendation == "wait"
        assert rec.submit_now_wait_sec is not None
        assert rec.best_window is not None
        assert rec.savings_sec is not None
        assert rec.savings_sec >= rec.savings_threshold_sec
        # Best window should fall inside the dip.
        assert 3 <= int(rec.best_window.submit_iso[11:13]) <= 6

    def test_dip_hour_recommends_now_when_already_cheap(self, tmp_path, monkeypatch):
        _seed_with_dip(tmp_path)
        # Pin now to a dip hour (Mon 04:30 UTC). "Now" already sits at
        # the floor of the diurnal pattern, so the next-24h sweep can't
        # beat it by the threshold.
        _pin_now(monkeypatch, datetime(2026, 4, 15, 4, 30, 0, tzinfo=timezone.utc))

        rec = recommend_resubmit_window(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            within_hours=24,
        )
        assert rec.recommendation == "now"
        assert rec.savings_sec is not None
        assert rec.savings_sec < rec.savings_threshold_sec

    def test_cold_start_returns_unknown(self, tmp_path):
        # No samples seeded → both predictors return cold-start.
        rec = recommend_resubmit_window(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
        )
        assert rec.recommendation == "unknown"
        assert rec.submit_now_wait_sec is None
        assert rec.best_window is None
        assert rec.savings_sec is None

    def test_threshold_gates_recommendation(self, tmp_path, monkeypatch):
        _seed_with_dip(tmp_path)
        _pin_now(monkeypatch, datetime(2026, 4, 15, 14, 0, 0, tzinfo=timezone.utc))

        # A threshold larger than the diurnal swing forces "now".
        rec = recommend_resubmit_window(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            within_hours=24,
            savings_threshold_sec=10_000,
        )
        assert rec.recommendation == "now"

    def test_to_dict_round_trips_best_window(self, tmp_path, monkeypatch):
        _seed_with_dip(tmp_path)
        _pin_now(monkeypatch, datetime(2026, 4, 15, 14, 0, 0, tzinfo=timezone.utc))

        rec = recommend_resubmit_window(tmp_path, profile=PROFILE, cluster=CLUSTER, within_hours=24)
        d = rec.to_dict()
        assert d["recommendation"] == "wait"
        assert d["best_window"] is not None
        assert "submit_iso" in d["best_window"]
        assert "predicted_wait_sec" in d["best_window"]
