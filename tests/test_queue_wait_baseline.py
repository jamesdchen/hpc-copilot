"""Tests for claude_hpc.forecast.queue_wait_baseline.predict_queue_wait."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_hpc.forecast import queue_wait_baseline as qwb
from claude_hpc.orchestrator import runtime_prior as rp


PROFILE = "ml_ridge"
CLUSTER = "discovery"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _seed_samples(tmp_path, samples):
    """Append a batch of (submitted_at_iso, queue_wait_sec, run_id_suffix?) triples."""
    for i, entry in enumerate(samples):
        sub_iso, wait_sec = entry[0], entry[1]
        rp.append_sample(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            run_id=f"r{i}",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso=sub_iso,
            queue_wait_sec=wait_sec,
        )


class TestColdStart:
    def test_empty_pool_returns_cold(self, tmp_path):
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
        )
        assert out.predicted_wait_sec is None
        assert out.confidence == "cold"
        assert out.method == "no_data"
        assert out.n_total_samples == 0
        assert out.bucket_hour_of_week >= 0  # parseable at_iso

    def test_below_global_threshold_is_cold(self, tmp_path):
        # 5 samples, threshold 20 → cold.
        base = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)
        _seed_samples(
            tmp_path,
            [(_iso(base + timedelta(minutes=i)), 600 + i) for i in range(5)],
        )
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:30:00+00:00",
        )
        assert out.predicted_wait_sec is None
        assert out.confidence == "cold"
        assert out.method == "no_data"
        assert out.n_total_samples == 5
        assert "need 20" in (out.fallback_reason or "")

    def test_unparseable_at_iso_is_cold(self, tmp_path):
        out = qwb.predict_queue_wait(
            tmp_path, profile=PROFILE, cluster=CLUSTER, at_iso="not-a-date"
        )
        assert out.predicted_wait_sec is None
        assert out.confidence == "cold"
        assert out.method == "no_data"
        assert out.bucket_hour_of_week == -1
        assert out.fallback_reason == "at_iso unparseable"


class TestDiurnalMA:
    def test_dense_target_bucket_returns_weighted_mean(self, tmp_path):
        # All samples submitted at the same hour-of-week (Tue 10:00 UTC).
        # 30 samples in target bucket → "high" confidence (>= 4 * 5).
        # Use the same week so exponential decay is similar across them
        # — the weighted mean should still be near the arithmetic mean.
        base = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)  # Tue 10:00
        seeds = [
            (_iso(base + timedelta(minutes=i)), 600) for i in range(30)
        ]
        _seed_samples(tmp_path, seeds)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso=_iso(base + timedelta(minutes=45)),
        )
        assert out.method == "diurnal_ma"
        assert out.confidence == "high"
        assert out.predicted_wait_sec == 600
        assert out.n_bucket_samples == 30
        assert out.n_total_samples == 30
        # Tue (weekday=1) at hour 10 → bucket = 1 * 24 + 10 = 34
        assert out.bucket_hour_of_week == 34
        assert out.fallback_reason is None

    def test_medium_confidence_when_between_thresholds(self, tmp_path):
        # 20 global samples. Put 6 in target bucket (>= 5 but < 20),
        # rest in unrelated buckets so blend not used.
        target = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)  # Tue 10
        # +12 hours → Tue 22 (different bucket, far from neighbours of 10)
        far = datetime(2026, 4, 28, 22, 0, 0, tzinfo=timezone.utc)
        seeds = [(_iso(target + timedelta(minutes=i)), 500) for i in range(6)]
        seeds += [(_iso(far + timedelta(minutes=i)), 9999) for i in range(14)]
        _seed_samples(tmp_path, seeds)
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso=_iso(target + timedelta(minutes=30)),
        )
        assert out.method == "diurnal_ma"
        assert out.confidence == "medium"
        assert out.predicted_wait_sec == 500
        assert out.n_bucket_samples == 6


class TestBlendedFallback:
    def test_sparse_target_blends_with_neighbours(self, tmp_path):
        # 1 sample in target bucket (Tue 10:00), 4 samples in neighbour
        # buckets (Tue 09:00 and Tue 11:00). Total 5 in the ±1h window
        # → meets blend threshold. Plus 15 distant samples to clear the
        # global gate. Distant bucket: Sat 03:00 (weekday=5, hour=3 →
        # bucket 123), nowhere near 33/34/35.
        target = datetime(2026, 4, 28, 10, 30, 0, tzinfo=timezone.utc)  # Tue 10
        prev = datetime(2026, 4, 28, 9, 30, 0, tzinfo=timezone.utc)  # Tue 09
        nxt = datetime(2026, 4, 28, 11, 30, 0, tzinfo=timezone.utc)  # Tue 11
        far = datetime(2026, 5, 2, 3, 0, 0, tzinfo=timezone.utc)  # Sat 03

        seeds = [(_iso(target), 600)]
        seeds += [(_iso(prev + timedelta(minutes=i)), 500) for i in range(2)]
        seeds += [(_iso(nxt + timedelta(minutes=i)), 700) for i in range(2)]
        seeds += [(_iso(far + timedelta(minutes=i)), 9999) for i in range(15)]
        _seed_samples(tmp_path, seeds)

        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso=_iso(target + timedelta(minutes=15)),
        )
        assert out.method == "blended_ma"
        assert out.confidence == "low"
        # Mean of {600, 500, 500, 700, 700} ≈ 600 (with light decay weighting).
        assert out.predicted_wait_sec is not None
        assert 500 <= out.predicted_wait_sec <= 700
        assert out.n_bucket_samples == 5
        assert "blended" in (out.fallback_reason or "")
        assert out.bucket_hour_of_week == 34

    def test_global_fallback_when_neighbours_also_sparse(self, tmp_path):
        # 1 sample in target bucket (Tue 10), 0 in neighbour buckets,
        # 19 in a distant bucket → blend window has only 1 (< 5), so we
        # fall back to global.
        target = datetime(2026, 4, 28, 10, 30, 0, tzinfo=timezone.utc)
        far = datetime(2026, 5, 2, 3, 0, 0, tzinfo=timezone.utc)  # Sat 03

        seeds = [(_iso(target), 600)]
        seeds += [(_iso(far + timedelta(minutes=i)), 1200) for i in range(19)]
        _seed_samples(tmp_path, seeds)

        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso=_iso(target + timedelta(minutes=15)),
        )
        assert out.method == "global_ma"
        assert out.confidence == "low"
        # Mean of 1×600 + 19×1200 ≈ 1170 (decay slightly tilts it).
        assert out.predicted_wait_sec is not None
        assert 1000 <= out.predicted_wait_sec <= 1200
        assert out.n_bucket_samples == 20  # all populated samples flat
        assert out.fallback_reason is not None


class TestPredictionResult:
    def test_to_dict_round_trip(self, tmp_path):
        out = qwb.predict_queue_wait(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            at_iso="2026-04-28T10:00:00+00:00",
        )
        d = out.to_dict()
        assert set(d) == {
            "predicted_wait_sec",
            "confidence",
            "method",
            "n_bucket_samples",
            "n_total_samples",
            "bucket_hour_of_week",
            "fallback_reason",
            # Phase 1c: order-book adjustment factor. Defaults to 1.0
            # when current_features is not supplied.
            "features_adjustment_factor",
            # Phase 4c: DES distribution fields. None on diurnal_ma path.
            "p10_wait_sec",
            "p90_wait_sec",
            "n_replications",
        }


# ---------------------------------------------------------------------------
# DES backend wiring (Phase 4c)
# ---------------------------------------------------------------------------


from claude_hpc.infra.inspect import (
    ClusterSnapshot, NodeSnapshot, persist_snapshot,
)


def _persist_idle_snapshot(tmp_path):
    """Write a minimal one-node IDLE snapshot under .hpc/cluster_history/<cluster>/."""
    n = NodeSnapshot(
        name="n0", state="IDLE", real_mem_mb=64_000, alloc_mem_mb=0,
        cpu_tot=8, cpu_alloc=0, gres="", gres_used="", co_tenants=[],
        is_drained=False,
    )
    snap = ClusterSnapshot(
        cluster=CLUSTER, scheduler_kind="slurm",
        now_iso="2026-04-28T10:00:00+00:00", nodes=[n],
    )
    persist_snapshot(tmp_path, snap)


class TestDESBackend:
    def test_des_explicit_idle_cluster_zero_wait(self, tmp_path):
        _persist_idle_snapshot(tmp_path)
        out = qwb.predict_queue_wait(
            tmp_path, profile=PROFILE, cluster=CLUSTER, backend="des",
            n_replications=4, seed=1,
        )
        assert out.method == "des"
        assert out.predicted_wait_sec == 0
        assert out.p10_wait_sec == 0
        assert out.p90_wait_sec == 0
        assert out.n_replications == 4

    def test_des_no_snapshot_falls_back_to_diurnal(self, tmp_path):
        # Empty experiment dir → DES should fall back, tag method.
        out = qwb.predict_queue_wait(
            tmp_path, profile=PROFILE, cluster=CLUSTER, backend="des",
            at_iso="2026-04-28T10:00:00+00:00",
        )
        assert out.method == "des_no_snapshot"
        # No history → still cold; method tag tells the caller why.
        assert out.predicted_wait_sec is None

    def test_auto_falls_back_when_no_snapshot(self, tmp_path):
        # No snapshot + no profiles → auto picks diurnal_ma path.
        out = qwb.predict_queue_wait(
            tmp_path, profile=PROFILE, cluster=CLUSTER, backend="auto",
            at_iso="2026-04-28T10:00:00+00:00",
        )
        assert out.method in ("no_data", "diurnal_ma", "blended_ma", "global_ma")

    def test_auto_picks_des_when_idle_snapshot_present(self, tmp_path):
        _persist_idle_snapshot(tmp_path)
        out = qwb.predict_queue_wait(
            tmp_path, profile=PROFILE, cluster=CLUSTER, backend="auto",
            n_replications=2, seed=1,
        )
        assert out.method == "des"
        assert out.predicted_wait_sec == 0

    def test_des_seed_determinism(self, tmp_path):
        _persist_idle_snapshot(tmp_path)
        kwargs = dict(profile=PROFILE, cluster=CLUSTER, backend="des",
                      n_replications=4, seed=42)
        a = qwb.predict_queue_wait(tmp_path, **kwargs)
        b = qwb.predict_queue_wait(tmp_path, **kwargs)
        assert a.predicted_wait_sec == b.predicted_wait_sec
        assert a.p10_wait_sec == b.p10_wait_sec
        assert a.p90_wait_sec == b.p90_wait_sec
