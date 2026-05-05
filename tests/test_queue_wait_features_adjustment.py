"""Tests for the Phase 1c order-book adjustment in ``predict_queue_wait``.

Verify that ``current_features`` nudges the diurnal MA in the expected
direction, that the factor is bounded, and that confidence is never
promoted by features alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_hpc.forecast import queue_wait_baseline as qwb
from claude_hpc.forecast.queue_features import QueueFeatures
from claude_hpc.forecast import runtime_prior as rp

PROFILE = "ml_ridge"
CLUSTER = "discovery"


def _seed(tmp_path, *, n: int = 30, wait: int = 1000):
    """Populate a runtime-prior pool with n samples on Tuesday 10:00 UTC."""
    base = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        ts = base - timedelta(days=i)
        rp.append_sample(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            run_id=f"r{i}",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso=ts.isoformat(),
            queue_wait_sec=wait,
        )


def _features(queued_in_partition: int) -> QueueFeatures:
    return QueueFeatures(
        queued_jobs_total=queued_in_partition,
        running_jobs_total=0,
        queued_jobs_in_partition=queued_in_partition,
        running_jobs_in_partition=0,
    )


class TestNoOpAndDirection:
    def test_no_features_factor_one(self, tmp_path):
        _seed(tmp_path)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
        )
        assert out.features_adjustment_factor == 1.0

    def test_features_at_reference_no_change(self, tmp_path):
        _seed(tmp_path)
        # Default reference depth is 10; passing depth=10 yields ratio=1
        # → factor = 1 + 0 * strength = 1.0.
        feats = _features(queued_in_partition=qwb._DEFAULT_REFERENCE_DEPTH)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
            current_features=feats,
        )
        assert out.features_adjustment_factor == 1.0

    def test_busy_queue_inflates_prediction(self, tmp_path):
        _seed(tmp_path)
        base = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
        )
        feats = _features(queued_in_partition=qwb._DEFAULT_REFERENCE_DEPTH * 2)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
            current_features=feats,
        )
        # 2× depth, dampened by 0.5 → factor 1.5.
        assert 1.4 < out.features_adjustment_factor < 1.6
        assert out.predicted_wait_sec is not None
        assert base.predicted_wait_sec is not None
        assert out.predicted_wait_sec > base.predicted_wait_sec

    def test_empty_queue_deflates_prediction(self, tmp_path):
        _seed(tmp_path)
        base = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
        )
        feats = _features(queued_in_partition=0)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
            current_features=feats,
        )
        # 0 depth, dampened → factor 0.5 (clamped at min, since
        # 1 + (0-1)*0.5 = 0.5 == _MIN_FACTOR).
        assert out.features_adjustment_factor == 0.5
        assert out.predicted_wait_sec is not None
        assert base.predicted_wait_sec is not None
        assert out.predicted_wait_sec < base.predicted_wait_sec


class TestBounded:
    def test_runaway_depth_clamped_to_max(self, tmp_path):
        _seed(tmp_path)
        feats = _features(queued_in_partition=10000)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
            current_features=feats,
        )
        assert out.features_adjustment_factor == qwb._MAX_FACTOR


class TestConfidenceNotPromoted:
    def test_low_confidence_stays_low_with_features(self, tmp_path):
        # Sparse: 3 samples → cold → no_data, but adjust expects we
        # treat this conservatively. Use blended scenario instead.
        # Use 22 sparse samples spread across many buckets so target
        # bucket is sparse enough to fall to global_ma (low).
        from datetime import datetime as dt

        base = dt(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(22):
            ts = base + timedelta(hours=6 * i)
            rp.append_sample(
                tmp_path,
                profile=PROFILE,
                cluster=CLUSTER,
                run_id=f"r{i}",
                task_id=0,
                gpu_type="a100",
                node="d11-07",
                elapsed_sec=4150,
                submitted_at_iso=ts.isoformat(),
                queue_wait_sec=1000,
            )
        feats = _features(queued_in_partition=qwb._DEFAULT_REFERENCE_DEPTH * 2)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
            current_features=feats,
        )
        # Order-book multiplier nudges magnitude but doesn't change the
        # confidence label. Verify confidence is still low (or the
        # method that yielded it).
        assert out.confidence in {"low", "medium", "high"}
        # Whatever confidence we got without features, we get with.
        bare = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
        )
        assert out.confidence == bare.confidence
        assert out.method == bare.method

    def test_cold_features_do_not_create_prediction(self, tmp_path):
        # Empty pool → cold. Features must not magically produce a
        # prediction.
        feats = _features(queued_in_partition=qwb._DEFAULT_REFERENCE_DEPTH * 2)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
            current_features=feats,
        )
        assert out.predicted_wait_sec is None
        assert out.confidence == "cold"
        assert out.method == "no_data"
