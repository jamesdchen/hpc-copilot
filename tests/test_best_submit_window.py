"""Tests for ``claude_hpc.forecast.best_submit_window.best_submit_windows``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_hpc.orchestrator import runtime_prior as rp
from claude_hpc.forecast.best_submit_window import best_submit_windows

PROFILE = "ml_ridge"
CLUSTER = "discovery"


def _seed_with_dip(tmp_path):
    """Seed 14 days of samples with a wide low-traffic dip at 03-06 UTC.

    Hours 03-06 UTC every day get a much shorter queue wait. The
    window is wide enough that the diurnal predictor's ±1h blend
    fallback still recovers a low predicted wait when the target
    bucket itself is sparse.
    """
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


class TestSweep:
    def test_low_traffic_window_surfaces_in_top_k(self, tmp_path, monkeypatch):
        _seed_with_dip(tmp_path)
        # Pin "now" to a deterministic value (Mon 14:00 UTC, 2026-04-15).
        # Sweep within 24h → must include the 04:00-05:00 dip.
        from claude_hpc.forecast import best_submit_window as bsw

        fixed_now = datetime(2026, 4, 15, 14, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(bsw, "utcnow", lambda: fixed_now)

        out = best_submit_windows(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            within_hours=24,
            top_k=5,
        )
        assert out
        # The 04:00 and 05:00 candidates sit inside the dip with both
        # neighbours also in the dip → those should be the lowest two.
        top_hours = {int(c.submit_iso[11:13]) for c in out[:2]}
        assert top_hours == {4, 5}
        # And those waits should be at the seeded low value (target
        # bucket dense + neighbours dense → diurnal_ma kicks in).
        assert all(c.predicted_wait_sec <= 200 for c in out[:2])

    def test_within_hours_zero_returns_empty(self, tmp_path):
        out = best_submit_windows(
            tmp_path, profile=PROFILE, cluster=CLUSTER, within_hours=0,
        )
        assert out == []

    def test_top_k_zero_returns_empty(self, tmp_path):
        _seed_with_dip(tmp_path)
        out = best_submit_windows(
            tmp_path, profile=PROFILE, cluster=CLUSTER, top_k=0,
        )
        assert out == []

    def test_cold_start_returns_empty(self, tmp_path):
        # No samples seeded → predictor returns no_data for every hour →
        # candidates list is empty.
        out = best_submit_windows(
            tmp_path, profile=PROFILE, cluster=CLUSTER, within_hours=12,
        )
        assert out == []

    def test_results_sorted_ascending(self, tmp_path, monkeypatch):
        _seed_with_dip(tmp_path)
        from claude_hpc.forecast import best_submit_window as bsw

        fixed_now = datetime(2026, 4, 15, 0, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(bsw, "utcnow", lambda: fixed_now)

        out = best_submit_windows(
            tmp_path, profile=PROFILE, cluster=CLUSTER,
            within_hours=24, top_k=10,
        )
        waits = [c.predicted_wait_sec for c in out]
        assert waits == sorted(waits)

    def test_to_dict_round_trip(self, tmp_path, monkeypatch):
        _seed_with_dip(tmp_path)
        from claude_hpc.forecast import best_submit_window as bsw

        fixed_now = datetime(2026, 4, 15, 0, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(bsw, "utcnow", lambda: fixed_now)

        out = best_submit_windows(
            tmp_path, profile=PROFILE, cluster=CLUSTER,
            within_hours=6, top_k=2,
        )
        assert out
        d = out[0].to_dict()
        assert set(d) == {
            "submit_iso", "predicted_wait_sec", "confidence",
            "method", "n_bucket_samples",
        }
