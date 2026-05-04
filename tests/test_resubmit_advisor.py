"""Tests for ``claude_hpc.forecast.resubmit_advisor.recommend_resubmit_window``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_hpc.forecast import best_submit_window as bsw
from claude_hpc.forecast import queue_wait_baseline as qwb
from claude_hpc.forecast.resubmit_advisor import recommend_resubmit_window
from claude_hpc.orchestrator import runtime_prior as rp

PROFILE = "ml_ridge"
CLUSTER = "discovery"


def _seed_with_dip(tmp_path):
    """14 days of hourly samples; hours 03-06 UTC have a 100s wait, others 1500s."""
    base = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    for day in range(14):
        for hour in range(24):
            for offset_min in (0, 30):
                ts = base + timedelta(days=day, hours=hour, minutes=offset_min)
                wait = 100 if 3 <= hour <= 6 else 1500
                rp.append_sample(
                    tmp_path,
                    profile=PROFILE,
                    cluster=CLUSTER,
                    run_id=f"r{day}-{hour}-{offset_min}",
                    task_id=0,
                    gpu_type="a100",
                    node="d11-07",
                    elapsed_sec=4150,
                    submitted_at_iso=ts.isoformat(),
                    queue_wait_sec=wait,
                )


def _pin_now(monkeypatch, dt: datetime) -> None:
    """Pin ``utcnow`` in both modules the advisor calls."""
    monkeypatch.setattr(bsw, "utcnow", lambda: dt)
    monkeypatch.setattr(qwb, "utcnow", lambda: dt)


class TestRecommendation:
    def test_busy_hour_recommends_wait_when_dip_is_within_horizon(
        self, tmp_path, monkeypatch
    ):
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

        rec = recommend_resubmit_window(
            tmp_path, profile=PROFILE, cluster=CLUSTER, within_hours=24
        )
        d = rec.to_dict()
        assert d["recommendation"] == "wait"
        assert d["best_window"] is not None
        assert "submit_iso" in d["best_window"]
        assert "predicted_wait_sec" in d["best_window"]
