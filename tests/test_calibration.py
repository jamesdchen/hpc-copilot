"""Tests for hpc_mapreduce.job.calibration — drift + house-edge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hpc_mapreduce.job import calibration as cal

# ─── walltime drift ────────────────────────────────────────────────────────


def _sample(elapsed: int, requested: int, exit_code: int = 0) -> dict:
    return {
        "elapsed_sec": elapsed,
        "walltime_requested_sec": requested,
        "exit_code": exit_code,
    }


class TestComputeWalltimeDrift:
    def test_empty_samples_returns_zero_drift(self):
        d = cal.compute_walltime_drift([])
        assert d.n_recent == 0
        assert d.weighted_cliff_rate == 0.0
        assert d.median_utilization is None

    def test_filters_samples_without_walltime(self):
        # Samples missing walltime_requested_sec contribute nothing.
        d = cal.compute_walltime_drift(
            [
                {"elapsed_sec": 1000, "walltime_requested_sec": 0, "exit_code": 0},
                {"elapsed_sec": 1000, "walltime_requested_sec": None, "exit_code": 0},
                _sample(1000, 2000),
            ]
        )
        assert d.n_recent == 1

    def test_cliff_event_requires_failure_exit(self):
        # elapsed/requested = 0.98, exit_code = 0 ⇒ near-miss not cliff.
        d = cal.compute_walltime_drift([_sample(980, 1000, exit_code=0)])
        assert d.n_cliff_events == 0
        assert d.n_near_misses == 1

    def test_cliff_event_with_failure_exit(self):
        # elapsed/requested = 0.99, exit_code = 1 ⇒ cliff.
        d = cal.compute_walltime_drift([_sample(990, 1000, exit_code=1)])
        assert d.n_cliff_events == 1
        assert d.n_near_misses == 0

    def test_weighted_rate_combines_cliff_and_near_miss(self):
        samples = [
            _sample(990, 1000, exit_code=1),  # cliff
            _sample(950, 1000, exit_code=0),  # near-miss
            _sample(500, 1000, exit_code=0),  # neither
            _sample(500, 1000, exit_code=0),  # neither
        ]
        d = cal.compute_walltime_drift(samples)
        # (1 + 0.5*1) / 4 = 0.375
        assert d.weighted_cliff_rate == 0.375
        assert d.median_utilization is not None

    def test_max_samples_truncates_oldest(self):
        # 200 cliff events, but max_samples=50 ⇒ rate is from the last 50,
        # which are still all cliffs in this synthetic case.
        samples = [_sample(990, 1000, exit_code=1)] * 200
        d = cal.compute_walltime_drift(samples, max_samples=50)
        assert d.n_recent == 50
        assert d.n_cliff_events == 50


class TestRecommendSafetyMultAdjustment:
    def test_below_min_samples_returns_base(self):
        d = cal.compute_walltime_drift([_sample(990, 1000, exit_code=1)] * 5)
        adj, rationale = cal.recommend_safety_mult_adjustment(
            d, base_safety_mult=1.30, min_samples_for_adjustment=10
        )
        assert adj == 1.30
        assert "insufficient drift signal" in rationale

    def test_loosens_above_threshold(self):
        # 20% cliff rate ⇒ 15% over threshold (5%) ⇒ 3 increments × 0.10 = +0.30
        samples = [_sample(990, 1000, exit_code=1)] * 20 + [_sample(500, 1000)] * 80
        d = cal.compute_walltime_drift(samples)
        adj, rationale = cal.recommend_safety_mult_adjustment(d, base_safety_mult=1.30)
        assert adj > 1.30
        assert "loosened" in rationale
        assert "1.30→" in rationale

    def test_ceiling_caps_loosening(self):
        # Catastrophic 100% cliff rate must not produce a multiplier above ceiling.
        samples = [_sample(990, 1000, exit_code=1)] * 50
        d = cal.compute_walltime_drift(samples)
        adj, _ = cal.recommend_safety_mult_adjustment(
            d, base_safety_mult=1.30, ceiling_safety_mult=2.00
        )
        assert adj <= 2.00

    def test_tightens_when_systematically_over_asking(self):
        # All jobs use ~30% of requested, zero cliffs ⇒ tighten.
        samples = [_sample(300, 1000, exit_code=0)] * 30
        d = cal.compute_walltime_drift(samples)
        adj, rationale = cal.recommend_safety_mult_adjustment(d, base_safety_mult=1.30)
        assert adj < 1.30
        assert "tightened" in rationale

    def test_no_adjustment_in_safe_zone(self):
        # 60% utilization, no cliffs ⇒ in the sweet spot.
        samples = [_sample(600, 1000, exit_code=0)] * 30
        d = cal.compute_walltime_drift(samples)
        adj, rationale = cal.recommend_safety_mult_adjustment(d, base_safety_mult=1.30)
        assert adj == 1.30
        assert "using base" in rationale


# ─── house edge ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _he_sample(predicted: int, actual_queue: int) -> dict:
    submitted = _now()
    started = submitted + timedelta(seconds=actual_queue)
    return {
        "predicted_eta_sec": predicted,
        "submitted_at_iso": submitted.isoformat(),
        "started_at_iso": started.isoformat(),
    }


class TestComputeHouseEdge:
    def test_empty_returns_zero(self):
        e = cal.compute_house_edge([])
        assert e.n_with_prediction == 0
        assert e.mean_delta_sec is None

    def test_skips_samples_without_prediction(self):
        e = cal.compute_house_edge(
            [
                {"predicted_eta_sec": None, "submitted_at_iso": "2026-01-01T00:00:00+00:00"},
                {"predicted_eta_sec": 0, "submitted_at_iso": "2026-01-01T00:00:00+00:00"},
                _he_sample(predicted=300, actual_queue=200),
            ]
        )
        assert e.n_with_prediction == 1

    def test_skips_samples_without_timestamps(self):
        e = cal.compute_house_edge([{"predicted_eta_sec": 300}])
        assert e.n_with_prediction == 0

    def test_negative_delta_when_scheduler_pessimistic(self):
        # Predicted 300s, actual was 100s ⇒ delta = -200s (we got in faster).
        e = cal.compute_house_edge([_he_sample(predicted=300, actual_queue=100)])
        assert e.n_with_prediction == 1
        assert e.mean_delta_sec is not None
        assert e.mean_delta_sec < 0

    def test_positive_delta_when_scheduler_optimistic(self):
        e = cal.compute_house_edge([_he_sample(predicted=100, actual_queue=400)])
        assert e.mean_delta_sec is not None
        assert e.mean_delta_sec > 0

    def test_calibration_ratio_aggregates(self):
        # actual/predicted = 0.5, 1.0, 1.5 ⇒ mean = 1.0 (well calibrated).
        samples = [
            _he_sample(predicted=200, actual_queue=100),
            _he_sample(predicted=200, actual_queue=200),
            _he_sample(predicted=200, actual_queue=300),
        ]
        e = cal.compute_house_edge(samples)
        assert e.calibration_ratio is not None
        assert abs(e.calibration_ratio - 1.0) < 0.01

    def test_skips_when_started_before_submitted(self):
        # Clock skew or bad data — we don't want to feed garbage into
        # the aggregate, so the function silently drops them.
        submitted = _now()
        started = submitted - timedelta(seconds=100)
        e = cal.compute_house_edge(
            [
                {
                    "predicted_eta_sec": 300,
                    "submitted_at_iso": submitted.isoformat(),
                    "started_at_iso": started.isoformat(),
                }
            ]
        )
        assert e.n_with_prediction == 0


# ─── prediction sidecar ────────────────────────────────────────────────────


class TestPredictionSidecar:
    def test_round_trip(self, tmp_path):
        path = cal.record_prediction_sidecar(
            tmp_path,
            run_id="r-123",
            predicted_eta_sec=300,
            constraint="a100",
            walltime_sec=1300,
            mem_mb=8192,
            cpus=4,
        )
        assert path.exists()
        doc = cal.read_prediction_sidecar(tmp_path, "r-123")
        assert doc is not None
        assert doc["predicted_eta_sec"] == 300
        assert doc["constraint"] == "a100"
        assert doc["walltime_sec"] == 1300

    def test_read_missing_returns_none(self, tmp_path):
        assert cal.read_prediction_sidecar(tmp_path, "no-such-run") is None

    def test_read_corrupt_returns_none(self, tmp_path):
        path = cal.prediction_sidecar_path(tmp_path, "r-bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")
        assert cal.read_prediction_sidecar(tmp_path, "r-bad") is None

    def test_overwrite_idempotent(self, tmp_path):
        cal.record_prediction_sidecar(
            tmp_path,
            run_id="r-1",
            predicted_eta_sec=100,
            constraint="a100",
            walltime_sec=600,
            mem_mb=4096,
            cpus=1,
        )
        cal.record_prediction_sidecar(
            tmp_path,
            run_id="r-1",
            predicted_eta_sec=200,  # changed
            constraint="a100",
            walltime_sec=600,
            mem_mb=4096,
            cpus=1,
        )
        doc = cal.read_prediction_sidecar(tmp_path, "r-1")
        assert doc is not None
        assert doc["predicted_eta_sec"] == 200

    def test_empty_run_id_raises(self, tmp_path):
        with pytest.raises(ValueError):
            cal.prediction_sidecar_path(tmp_path, "")
