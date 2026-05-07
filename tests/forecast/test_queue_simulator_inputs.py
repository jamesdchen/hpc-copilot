"""Tests for claude_hpc.forecast.queue_simulator_inputs (DES sampling helpers)."""

from __future__ import annotations

from claude_hpc.forecast.queue_simulator_inputs import (
    sample_arrival_stream,
    sample_residual_lifetimes,
)
from claude_hpc.infra.inspect import ClusterSnapshot, NodeSnapshot


class TestArrivalStream:
    def test_empty_profiles_returns_empty(self):
        assert sample_arrival_stream({}) == []

    def test_zero_rate_user_yields_no_arrivals(self):
        out = sample_arrival_stream(
            {"alice": {"median_submits_per_day": 0.0}},
            horizon_sec=86400.0,
            seed=1,
        )
        assert out == []

    def test_uniform_rate_average_count(self):
        # Theoretical mean: 10 submits/day × 1 day = 10. We allow ±5
        # tolerance over a single seed (Poisson variance ≈ √10 ≈ 3.2).
        # Repeat with several seeds and check the *average* is in
        # [7, 13] which is well within bounds.
        profiles = {
            "alice": {
                "median_submits_per_day": 10.0,
                "hour_of_week_factors": [1.0] * 168,
                "common_walltime_ask_sec": 600.0,
                "common_cpus": 4,
                "common_mem_mb": 8_000,
            }
        }
        totals = []
        for s in range(20):
            out = sample_arrival_stream(profiles, horizon_sec=86400.0, seed=s)
            totals.append(len(out))
        avg = sum(totals) / len(totals)
        assert 7 <= avg <= 13

    def test_arrivals_sorted_by_submit_time(self):
        profiles = {
            "u1": {"median_submits_per_day": 50.0},
            "u2": {"median_submits_per_day": 50.0},
        }
        out = sample_arrival_stream(profiles, horizon_sec=86400.0, seed=11)
        times = [j.submit_time for j in out]
        assert times == sorted(times)

    def test_jobs_carry_user_and_shape(self):
        profiles = {
            "alice": {
                "median_submits_per_day": 24.0,
                "common_walltime_ask_sec": 1200.0,
                "common_cpus": 8,
                "common_mem_mb": 32_000,
                "common_gpus": 1,
                "common_gpu_type": "a100",
            }
        }
        out = sample_arrival_stream(profiles, horizon_sec=86400.0, seed=3)
        assert out, "expected at least one arrival from rate 24/day"
        for j in out:
            assert j.user == "alice"
            assert j.cpus == 8
            assert j.mem_mb == 32_000
            assert j.gpus == 1
            assert j.gpu_type == "a100"
            assert j.walltime_ask == 1200.0

    def test_phase2a_userprofile_schema(self):
        # Real Phase-2a UserProfile uses median_walltime_ask_sec +
        # submit_hour_of_week_distribution + typical_gpu_types instead
        # of the early-draft common_* fields. The sampler must accept
        # either schema.
        profiles = {
            "bob": {
                "median_submits_per_day": 24.0,
                "median_walltime_ask_sec": 900,
                "submit_hour_of_week_distribution": {h: 1.0 / 168 for h in range(168)},
                "typical_gpu_types": ["v100"],
            }
        }
        out = sample_arrival_stream(profiles, horizon_sec=86400.0, seed=5)
        assert out, "expected at least one arrival under Phase-2a schema"
        assert all(j.walltime_ask == 900.0 for j in out)
        assert all(j.gpu_type == "v100" for j in out)


class TestResidualLifetimes:
    def _snap_with_running(self, elapsed_s=600, walltime_ask_default_extra=3600):
        n = NodeSnapshot(
            name="n0",
            state="ALLOCATED",
            real_mem_mb=64_000,
            alloc_mem_mb=32_000,
            cpu_tot=8,
            cpu_alloc=4,
            co_tenants=[
                {
                    "job_id": "jR",
                    "user": "alice",
                    "cpus": 4,
                    "mem_gb": 32,
                    "gpus": 0,
                    "elapsed_s": elapsed_s,
                    "state": "RUNNING",
                    "started_h_ago": elapsed_s / 3600.0,
                }
            ],
            is_drained=False,
        )
        return ClusterSnapshot(
            cluster="t",
            scheduler_kind="slurm",
            now_iso="2026-04-28T10:00:00+00:00",
            nodes=[n],
        )

    def test_default_profile_yields_nonneg_residual(self):
        snap = self._snap_with_running(elapsed_s=600)
        out = sample_residual_lifetimes(snap, None, seed=1)
        assert "jR" in out
        assert out["jR"] >= 0.0

    def test_residual_uses_user_ratio(self):
        # User profile says ratio is exactly 0.5 (triangular collapsed).
        snap = self._snap_with_running(elapsed_s=600)
        profiles = {
            "alice": {
                "actual_over_ask_p10": 0.5,
                "actual_over_ask_p90": 0.5,
                "median_actual_over_ask": 0.5,
            }
        }
        out = sample_residual_lifetimes(snap, profiles, seed=1)
        # walltime_ask = elapsed + 3600 = 4200; ratio 0.5 → total 2100;
        # residual = 2100 - 600 = 1500.
        assert abs(out["jR"] - 1500.0) < 1e-6

    def test_seed_determinism(self):
        snap = self._snap_with_running(elapsed_s=600)
        a = sample_residual_lifetimes(snap, None, seed=99)
        b = sample_residual_lifetimes(snap, None, seed=99)
        assert a == b
