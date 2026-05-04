"""Tests for claude_hpc.forecast.queue_simulator (DES core).

Covers the FIFO + EASY backfill scheduler invariants.
"""

from __future__ import annotations

from claude_hpc.infra.inspect import ClusterSnapshot, NodeSnapshot
from claude_hpc.forecast.queue_simulator import (
    SimJob,
    available_resources,
    extract_running_jobs,
    simulate_distribution,
    simulate_one_pass,
)


def _empty_snapshot(nodes=1, cpus=8, mem_mb=64_000, gpus=0, gpu_type="a100"):
    """Build a snapshot with `nodes` identical idle nodes."""
    ns: list[NodeSnapshot] = []
    for i in range(nodes):
        gres = f"gpu:{gpu_type}:{gpus}" if gpus else ""
        n = NodeSnapshot(
            name=f"n{i}",
            state="IDLE",
            real_mem_mb=mem_mb,
            alloc_mem_mb=0,
            cpu_tot=cpus,
            cpu_alloc=0,
            gres=gres,
            gres_used="",
            co_tenants=[],
            is_drained=False,
        )
        ns.append(n)
    return ClusterSnapshot(
        cluster="test",
        scheduler_kind="slurm",
        now_iso="2026-04-28T10:00:00+00:00",
        nodes=ns,
    )


def _full_snapshot(*, cpus=8, mem_mb=64_000, gpus=0, gpu_type="a100", elapsed_s=3600, user="alice"):
    """A snapshot where the single node is fully utilized by one job."""
    co = [
        {
            "job_id": "j_running",
            "user": user,
            "cpus": cpus,
            "mem_gb": mem_mb / 1024.0,
            "gpus": gpus,
            "elapsed_s": elapsed_s,
            "state": "RUNNING",
            "started_h_ago": elapsed_s / 3600.0,
        }
    ]
    gres = f"gpu:{gpu_type}:{gpus}" if gpus else ""
    gres_used = f"gpu:{gpu_type}:{gpus}" if gpus else ""
    n = NodeSnapshot(
        name="n0",
        state="ALLOCATED",
        real_mem_mb=mem_mb,
        alloc_mem_mb=mem_mb,
        cpu_tot=cpus,
        cpu_alloc=cpus,
        gres=gres,
        gres_used=gres_used,
        co_tenants=co,
        is_drained=False,
    )
    return ClusterSnapshot(
        cluster="test",
        scheduler_kind="slurm",
        now_iso="2026-04-28T10:00:00+00:00",
        nodes=[n],
    )


def _candidate(
    job_id="cand-1",
    cpus=4,
    mem_mb=8_000,
    gpus=0,
    gpu_type="",
    walltime_ask=600,
    submit_time=0.0,
    user="bob",
):
    return SimJob(
        job_id=job_id,
        user=user,
        submit_time=submit_time,
        walltime_ask=walltime_ask,
        cpus=cpus,
        mem_mb=mem_mb,
        gpus=gpus,
        gpu_type=gpu_type,
    )


class TestEmptyCluster:
    def test_candidate_starts_immediately(self):
        snap = _empty_snapshot()
        out = simulate_one_pass(snap, candidate=_candidate())
        assert out.predicted_start_offset_sec == 0.0
        assert out.candidate_job_id == "cand-1"

    def test_extract_running_jobs_empty(self):
        snap = _empty_snapshot()
        assert extract_running_jobs(snap) == []

    def test_available_resources_idle(self):
        snap = _empty_snapshot(cpus=8, mem_mb=64_000)
        free = available_resources(snap)
        assert free["n0"]["cpus_free"] == 8
        assert free["n0"]["mem_mb_free"] == 64_000


class TestFullCluster:
    def test_candidate_waits_for_running_job(self):
        # Running job has 1h elapsed and walltime_ask=elapsed+1h=2h, so ~1h
        # remaining when residual_lifetimes is not provided.
        snap = _full_snapshot(elapsed_s=3600)
        out = simulate_one_pass(snap, candidate=_candidate(cpus=4, mem_mb=8_000))
        assert 3500 < out.predicted_start_offset_sec < 3700

    def test_candidate_waits_for_residual_lifetime_override(self):
        snap = _full_snapshot()
        out = simulate_one_pass(
            snap,
            candidate=_candidate(cpus=4, mem_mb=8_000),
            residual_lifetimes={"j_running": 1234.0},
        )
        assert out.predicted_start_offset_sec == 1234.0


class TestFIFOPriority:
    def test_two_candidates_compete(self):
        # Cluster has 8 CPUs free; two jobs each need 8 CPUs. The second
        # arrival queues behind the first.
        snap = _empty_snapshot(cpus=8)
        first = SimJob(
            job_id="j1",
            user="u",
            submit_time=0.0,
            walltime_ask=500,
            cpus=8,
            mem_mb=1_000,
        )
        second = SimJob(
            job_id="j2",
            user="u",
            submit_time=10.0,
            walltime_ask=300,
            cpus=8,
            mem_mb=1_000,
        )
        out = simulate_one_pass(
            snap,
            candidate=second,
            arrival_stream=[first],
        )
        # j1 starts at t=0 with walltime jittered 0.6-1.0 of 500. j2
        # waits for j1 to finish; wait = end_of_j1 - submit(10).
        assert out.predicted_start_offset_sec > 290


class TestEASYBackfill:
    def test_small_candidate_backfills_in_gap(self):
        # Setup: 8-CPU node MIXED (4 used by a running job). A queued
        # 8-CPU "blocker" cannot start until the running job ends.
        # The candidate needs only 4 CPUs and 60s — it must backfill in
        # the gap before the blocker's reservation.
        n = NodeSnapshot(
            name="n0",
            state="MIXED",
            real_mem_mb=64_000,
            alloc_mem_mb=32_000,
            cpu_tot=8,
            cpu_alloc=4,
            co_tenants=[
                {
                    "job_id": "j_big_running",
                    "user": "u",
                    "cpus": 4,
                    "mem_gb": 32,
                    "gpus": 0,
                    "elapsed_s": 3000,
                    "state": "RUNNING",
                    "started_h_ago": 1.0,
                }
            ],
            is_drained=False,
        )
        snap = ClusterSnapshot(
            cluster="t",
            scheduler_kind="slurm",
            now_iso="2026-04-28T10:00:00+00:00",
            nodes=[n],
        )
        blocker = SimJob(
            job_id="blocker",
            user="u",
            submit_time=0.0,
            walltime_ask=1000,
            cpus=8,
            mem_mb=8_000,
            walltime_actual=1000.0,
        )
        candidate = _candidate(cpus=4, mem_mb=8_000, walltime_ask=60, submit_time=0.0)
        out = simulate_one_pass(
            snap,
            candidate=candidate,
            arrival_stream=[blocker],
            residual_lifetimes={"j_big_running": 600.0},
        )
        assert out.predicted_start_offset_sec == 0.0


class TestDeterminism:
    def test_same_seed_same_result(self):
        snap = _empty_snapshot(cpus=8)
        arrivals = [
            SimJob(
                job_id=f"a{i}",
                user="u",
                submit_time=float(i * 10),
                walltime_ask=200,
                cpus=4,
                mem_mb=1_000,
            )
            for i in range(5)
        ]
        c = _candidate(cpus=4, walltime_ask=100, submit_time=25.0)
        a = simulate_one_pass(snap, candidate=c, arrival_stream=list(arrivals), seed=42)
        b = simulate_one_pass(snap, candidate=c, arrival_stream=list(arrivals), seed=42)
        assert a.predicted_start_offset_sec == b.predicted_start_offset_sec

    def test_distribution_n1_matches_one_pass(self):
        snap = _empty_snapshot()
        c = _candidate()
        a = simulate_one_pass(snap, candidate=c, seed=1)
        b = simulate_distribution(snap, candidate=c, n_replications=1, seed=1)
        assert b.p50_wait_sec == a.predicted_start_offset_sec


class TestDistribution:
    def test_distribution_returns_quantile_ladder(self):
        snap = _empty_snapshot(cpus=8)
        c = _candidate(cpus=8, mem_mb=8_000)
        arrivals = [
            SimJob(
                job_id=f"a{i}", user="u", submit_time=0.0, walltime_ask=400, cpus=8, mem_mb=8_000
            )
            for i in range(3)
        ]
        out = simulate_distribution(
            snap,
            candidate=c,
            n_replications=8,
            seed=7,
            arrival_sampler=lambda s: list(arrivals),
        )
        assert out.n_replications == 8
        assert out.p10_wait_sec <= out.p50_wait_sec <= out.p90_wait_sec
