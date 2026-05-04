"""Tests for ``hpc_mapreduce.job.queue_features.compute_features``."""

from __future__ import annotations

from hpc_mapreduce._time import utcnow_iso
from hpc_mapreduce.infra.inspect import ClusterSnapshot, NodeSnapshot
from hpc_mapreduce.job.queue_features import compute_features


def _node(
    name: str,
    *,
    gres: str = "",
    gres_used: str = "",
    cpu_tot: int | None = None,
    cpu_alloc: int | None = None,
    real_mem_mb: int | None = None,
    alloc_mem_mb: int | None = None,
    co_tenants: list[dict] | None = None,
    is_drained: bool = False,
) -> NodeSnapshot:
    return NodeSnapshot(
        name=name,
        gres=gres,
        gres_used=gres_used,
        cpu_tot=cpu_tot,
        cpu_alloc=cpu_alloc,
        real_mem_mb=real_mem_mb,
        alloc_mem_mb=alloc_mem_mb,
        co_tenants=co_tenants or [],
        is_drained=is_drained,
    )


def _snap(nodes, *, now_iso: str | None = None) -> ClusterSnapshot:
    return ClusterSnapshot(
        cluster="discovery",
        scheduler_kind="slurm",
        now_iso=now_iso or utcnow_iso(),
        nodes=list(nodes),
    )


class TestSupplyDemand:
    def test_typed_supply_and_running(self):
        snap = _snap([
            _node("a", gres="gpu:a100:2", gres_used="gpu:a100:1"),
            _node("b", gres="gpu:a100:1,gpu:v100:2", gres_used="gpu:v100:1"),
        ])
        f = compute_features(snap)
        assert f.gpu_type_supply == {"a100": 3, "v100": 2}
        assert f.gpu_type_running == {"a100": 1, "v100": 1}

    def test_untyped_gres_buckets_under_unknown(self):
        snap = _snap([
            _node("a", gres="gpu:2", gres_used="gpu:1"),
        ])
        f = compute_features(snap)
        assert f.gpu_type_supply == {"unknown": 2}
        assert f.gpu_type_running == {"unknown": 1}

    def test_drained_nodes_excluded(self):
        snap = _snap([
            _node("a", gres="gpu:a100:2", gres_used="gpu:a100:1"),
            _node("b", gres="gpu:a100:4", is_drained=True),
        ])
        f = compute_features(snap)
        assert f.gpu_type_supply == {"a100": 2}


class TestQueueDepth:
    def test_queued_and_running_counts(self):
        snap = _snap([
            _node(
                "a",
                co_tenants=[
                    {"user": "alice", "state": "RUNNING"},
                    {"user": "bob", "state": "PD", "gpus": 2},
                ],
            ),
            _node(
                "b",
                co_tenants=[
                    {"user": "alice", "state": "RUNNING"},
                    {"user": "carol", "state": "PD"},
                ],
            ),
        ])
        f = compute_features(snap)
        assert f.queued_jobs_total == 2
        assert f.running_jobs_total == 2
        assert f.gpu_type_queued_demand == {"unknown": 2}

    def test_partition_falls_back_when_metadata_absent(self):
        snap = _snap([
            _node("a", co_tenants=[{"user": "alice", "state": "RUNNING"}]),
        ])
        f = compute_features(snap, partition="gpu_p100")
        # No partitions on nodes — feature falls back to cluster-wide.
        assert f.running_jobs_in_partition == 1
        assert f.queued_jobs_in_partition == 0


class TestPressure:
    def test_cpu_and_mem_pct_weighted(self):
        snap = _snap([
            _node("a", cpu_tot=8, cpu_alloc=4, real_mem_mb=16000, alloc_mem_mb=8000),
            _node("b", cpu_tot=8, cpu_alloc=8, real_mem_mb=16000, alloc_mem_mb=4000),
        ])
        f = compute_features(snap)
        # 12/16 cores = 0.75
        assert f.cpus_in_use_pct == 0.75
        # 12000/32000 = 0.375
        assert f.mem_in_use_pct == 0.375


class TestUsersAndAge:
    def test_unique_running_users(self):
        snap = _snap([
            _node(
                "a",
                co_tenants=[
                    {"user": "alice", "state": "RUNNING"},
                    {"user": "bob", "state": "RUNNING"},
                    {"user": "alice", "state": "RUNNING"},  # dup
                ],
            ),
        ])
        f = compute_features(snap)
        assert f.n_unique_users_running == 2

    def test_snapshot_age_clamps_negative_to_zero(self):
        # Future-dated snapshot (clock skew) should still yield 0 age.
        snap = _snap([], now_iso="2099-01-01T00:00:00")
        f = compute_features(snap)
        assert f.snapshot_age_sec == 0


class TestEdgeCases:
    def test_empty_snapshot(self):
        snap = _snap([])
        f = compute_features(snap)
        assert f.queued_jobs_total == 0
        assert f.running_jobs_total == 0
        assert f.gpu_type_supply == {}
        assert f.cpus_in_use_pct == 0.0
        assert f.n_unique_users_running == 0
