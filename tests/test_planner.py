"""Tests for hpc_mapreduce.job.planner — integration via mocked snapshot."""

from __future__ import annotations

from unittest.mock import patch

from hpc_mapreduce.infra.inspect import ClusterSnapshot, NodeSnapshot
from hpc_mapreduce.job import blacklist as bl
from hpc_mapreduce.job import planner
from hpc_mapreduce.job import runtime_prior as rp


def _write_clusters(tmp_path, scheduler="slurm"):
    p = tmp_path / "clusters.yaml"
    p.write_text(
        "discovery:\n"
        "  host: example.invalid\n"
        "  user: tester\n"
        f"  scheduler: {scheduler}\n"
        "  scratch: /tmp\n"
        "  gpu_types: [a100, v100]\n"
    )
    return p


def _fake_snapshot():
    """Two-node snapshot: one healthy, one stressed."""
    healthy = NodeSnapshot(
        name="d11-07",
        state="MIXED",
        real_mem_mb=192000,
        alloc_mem_mb=64000,
        alloc_mem_pct=0.33,
        cpu_tot=32,
        cpu_load=3.2,
        cpu_load_frac=0.10,
        gres="gpu:a100:2",
        gres_used="gpu:a100:0",
        active_features=["a100"],
        is_stressed=False,
        is_drained=False,
    )
    stressed = NodeSnapshot(
        name="d11-03",
        state="MIXED",
        real_mem_mb=192000,
        alloc_mem_mb=170000,
        alloc_mem_pct=0.88,
        cpu_tot=32,
        cpu_load=22.0,
        cpu_load_frac=0.69,
        gres="gpu:v100:2",
        gres_used="gpu:v100:1",
        active_features=["v100"],
        co_tenants=[
            {"user": "alice", "job_id": "1", "cpus": 24, "mem_gb": 128, "started_h_ago": 19}
        ],
        is_stressed=True,
        is_drained=False,
    )
    return ClusterSnapshot(
        cluster="discovery",
        scheduler_kind="slurm",
        now_iso="2026-01-01T00:00:00+00:00",
        nodes=[healthy, stressed],
    )


class TestPlanSubmit:
    def test_needs_canary_when_no_priors(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch("hpc_mapreduce.job.planner.inspect_cluster", return_value=_fake_snapshot()):
            out = planner.plan_submit(
                tmp_path, profile="ml_ridge", cluster="discovery", candidates=["a100"]
            )
        assert out["needs_canary"] is True
        assert out["canary_plan"] is not None
        assert out["canary_plan"]["constraint"] == "a100"

    def test_candidates_include_default_pair(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch("hpc_mapreduce.job.planner.inspect_cluster", return_value=_fake_snapshot()):
            out = planner.plan_submit(tmp_path, profile="x", cluster="discovery")
        constraints = [c["constraint"] for c in out["candidates"]]
        # Default behavior: each gpu type + the union.
        assert "a100" in constraints
        assert "v100" in constraints
        assert "a100|v100" in constraints

    def test_blacklisted_node_separated_from_pool(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-07",
            run_id="r-prior",
            job_id="500",
            task_id=0,
        )
        with patch("hpc_mapreduce.job.planner.inspect_cluster", return_value=_fake_snapshot()):
            out = planner.plan_submit(
                tmp_path, profile="x", cluster="discovery", candidates=["a100"]
            )
        a100 = next(c for c in out["candidates"] if c["constraint"] == "a100")
        assert a100["healthy_nodes"] == []
        assert len(a100["blacklisted_nodes"]) == 1
        assert a100["blacklisted_nodes"][0]["node"] == "d11-07"

    def test_stressed_node_surfaces_co_tenants(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch("hpc_mapreduce.job.planner.inspect_cluster", return_value=_fake_snapshot()):
            out = planner.plan_submit(
                tmp_path, profile="x", cluster="discovery", candidates=["v100"]
            )
        v100 = out["candidates"][0]
        assert v100["healthy_nodes"] == []
        assert len(v100["stressed_nodes"]) == 1
        s = v100["stressed_nodes"][0]
        assert s["node"] == "d11-03"
        assert s["AllocMem_pct"] == 0.88
        assert s["co_tenants"][0]["user"] == "alice"

    def test_with_priors_returns_quantiles(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        for tid in range(5):
            rp.append_sample(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                run_id="r1",
                task_id=tid,
                gpu_type="a100",
                node="d11-07",
                elapsed_sec=1000 + tid * 100,
            )
        with patch("hpc_mapreduce.job.planner.inspect_cluster", return_value=_fake_snapshot()):
            out = planner.plan_submit(
                tmp_path, profile="ml_ridge", cluster="discovery", candidates=["a100"]
            )
        assert out["needs_canary"] is False
        c = out["candidates"][0]
        assert c["runtime_prior_quantiles_sec"]["a100"]["n_samples"] == 5


class TestTestOnlyEtaParser:
    def test_parses_iso_timestamp(self):
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        text = f"sbatch: Job 12345 to start at {future} using 1 ..."
        eta = planner._parse_test_only_eta(text)
        assert eta is not None
        assert 0 < eta < 700

    def test_unparseable_returns_none(self):
        assert planner._parse_test_only_eta("") is None
        assert planner._parse_test_only_eta("submission failed") is None
