"""Tests for ``claude_hpc.forecast.state_forecast.forecast_state_at``."""

from __future__ import annotations

from claude_hpc._internal._time import utcnow_iso
from claude_hpc.forecast.state_forecast import forecast_state_at
from claude_hpc.infra.inspect import ClusterSnapshot, NodeSnapshot
from claude_hpc.state.user_profiles import UserProfile


def _node(
    name: str = "d11-07",
    *,
    gres: str = "gpu:a100:4",
    cpu_tot: int = 32,
    real_mem_mb: int = 256000,
    co_tenants: list[dict] | None = None,
) -> NodeSnapshot:
    return NodeSnapshot(
        name=name,
        gres=gres,
        cpu_tot=cpu_tot,
        real_mem_mb=real_mem_mb,
        co_tenants=co_tenants or [],
    )


def _snap(nodes) -> ClusterSnapshot:
    return ClusterSnapshot(
        cluster="discovery",
        scheduler_kind="slurm",
        now_iso=utcnow_iso(),
        nodes=list(nodes),
    )


class TestEmptyAndIdle:
    def test_no_co_tenants_equals_full_capacity(self):
        snap = _snap([_node()])
        out = forecast_state_at(snap, t_offset_sec=3600)
        assert out.available_gpus == 4
        assert out.available_cpus == 32
        assert out.n_jobs_completing_by_t == 0

    def test_t_offset_zero_returns_current(self):
        snap = _snap(
            [
                _node(
                    co_tenants=[
                        {
                            "user": "alice",
                            "state": "RUNNING",
                            "cpus": 8,
                            "mem_gb": 32,
                            "gpus": 1,
                            "elapsed_s": 100,
                            "walltime_ask_sec": 3600,
                        }
                    ]
                )
            ]
        )
        out = forecast_state_at(snap, t_offset_sec=0)
        assert out.available_gpus == 3
        assert out.available_cpus == 24
        assert out.n_jobs_completing_by_t == 0


class TestCompletionsByT:
    def test_long_running_jobs_complete_by_large_t(self):
        prof = UserProfile(
            user="alice",
            n_observations=50,
            median_actual_over_ask=0.5,
        )
        snap = _snap(
            [
                _node(
                    co_tenants=[
                        {
                            "user": "alice",
                            "state": "RUNNING",
                            "cpus": 8,
                            "mem_gb": 32,
                            "gpus": 1,
                            "elapsed_s": 100,
                            "walltime_ask_sec": 1000,
                        }
                    ]
                )
            ]
        )
        out = forecast_state_at(
            snap,
            t_offset_sec=1000,
            profiles={"alice": prof},
        )
        assert out.n_jobs_completing_by_t == 1
        assert out.available_gpus == 4
        assert out.available_cpus == 32

    def test_jobs_outliving_t_not_counted(self):
        prof = UserProfile(
            user="alice",
            n_observations=50,
            median_actual_over_ask=1.0,
        )
        snap = _snap(
            [
                _node(
                    co_tenants=[
                        {
                            "user": "alice",
                            "state": "RUNNING",
                            "cpus": 8,
                            "mem_gb": 32,
                            "gpus": 1,
                            "elapsed_s": 0,
                            "walltime_ask_sec": 10000,
                        }
                    ]
                )
            ]
        )
        out = forecast_state_at(
            snap,
            t_offset_sec=100,
            profiles={"alice": prof},
        )
        assert out.n_jobs_completing_by_t == 0
        assert out.available_gpus == 3

    def test_mid_run_partial_availability(self):
        prof_short = UserProfile(
            user="alice",
            n_observations=50,
            median_actual_over_ask=0.4,
        )
        prof_long = UserProfile(
            user="bob",
            n_observations=50,
            median_actual_over_ask=1.0,
        )
        snap = _snap(
            [
                _node(
                    co_tenants=[
                        {
                            "user": "alice",
                            "state": "RUNNING",
                            "cpus": 4,
                            "mem_gb": 16,
                            "gpus": 1,
                            "elapsed_s": 200,
                            "walltime_ask_sec": 1000,
                        },
                        {
                            "user": "bob",
                            "state": "RUNNING",
                            "cpus": 4,
                            "mem_gb": 16,
                            "gpus": 1,
                            "elapsed_s": 0,
                            "walltime_ask_sec": 10000,
                        },
                    ]
                )
            ]
        )
        out = forecast_state_at(
            snap,
            t_offset_sec=1000,
            profiles={"alice": prof_short, "bob": prof_long},
        )
        assert out.n_jobs_completing_by_t == 1
        assert out.available_gpus == 3


class TestColdStart:
    def test_unprofiled_user_uses_fallback(self):
        snap = _snap(
            [
                _node(
                    co_tenants=[
                        {
                            "user": "stranger",
                            "state": "RUNNING",
                            "cpus": 4,
                            "mem_gb": 16,
                            "gpus": 1,
                            "elapsed_s": 0,
                            "walltime_ask_sec": 1000,
                        }
                    ]
                )
            ]
        )
        # residual = 850. T = 900 > 850 → completes.
        out = forecast_state_at(snap, t_offset_sec=900)
        assert out.n_jobs_completing_by_t == 1


class TestEdge:
    def test_drained_node_excluded_from_capacity(self):
        snap = _snap(
            [
                NodeSnapshot(
                    name="x",
                    gres="gpu:8",
                    cpu_tot=64,
                    real_mem_mb=256000,
                    is_drained=True,
                ),
                _node(),
            ]
        )
        out = forecast_state_at(snap, t_offset_sec=0)
        assert out.available_gpus == 4
        assert out.available_cpus == 32

    def test_pending_job_does_not_consume_allocation(self):
        snap = _snap(
            [
                _node(
                    co_tenants=[
                        {
                            "user": "alice",
                            "state": "PD",
                            "cpus": 8,
                            "mem_gb": 32,
                            "gpus": 2,
                        }
                    ]
                )
            ]
        )
        out = forecast_state_at(snap, t_offset_sec=0)
        assert out.available_gpus == 4
        assert out.available_cpus == 32

    def test_missing_walltime_treated_as_running(self):
        snap = _snap(
            [
                _node(
                    co_tenants=[
                        {
                            "user": "alice",
                            "state": "RUNNING",
                            "cpus": 8,
                            "mem_gb": 32,
                            "gpus": 1,
                            "elapsed_s": 100,
                        }
                    ]
                )
            ]
        )
        out = forecast_state_at(snap, t_offset_sec=10000)
        assert out.n_jobs_completing_by_t == 0
        assert out.available_gpus == 3
