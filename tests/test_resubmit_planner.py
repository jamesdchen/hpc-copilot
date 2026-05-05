"""Tests for ``claude_hpc.orchestrator.planning.resubmit_planner.plan_resubmit_overrides``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_hpc.forecast import runtime_prior as rp
from claude_hpc.orchestrator.planning.resubmit_planner import (
    MIN_PRIOR_SAMPLES,
    plan_resubmit_overrides,
)

PROFILE = "ml_ridge"
CLUSTER = "test_cluster"


def _write_clusters_yaml(
    tmp_path,
    monkeypatch,
    *,
    cold_start_mem_buffer: float | None = 0.15,
    walltime_arbitrage: bool | None = True,
    auto_daisy_chain: bool | None = True,
    max_walltime_sec: int = 86400,
    max_node_mem_mb: int | None = 256_000,
):
    """Write a tmp clusters.yaml with the given knobs and point loader at it."""
    cfg: dict[str, object] = {
        "scheduler": "slurm",
        "ssh_target": "test@cluster.example.edu",
        "max_walltime_sec": max_walltime_sec,
    }
    if cold_start_mem_buffer is not None:
        cfg["cold_start_mem_buffer"] = cold_start_mem_buffer
    if walltime_arbitrage is not None:
        cfg["walltime_arbitrage"] = walltime_arbitrage
    if auto_daisy_chain is not None:
        cfg["auto_daisy_chain"] = auto_daisy_chain
    if max_node_mem_mb is not None:
        cfg["max_node_mem_mb"] = max_node_mem_mb

    yaml_path = tmp_path / "clusters.yaml"
    yaml_path.write_text(_dump_yaml({CLUSTER: cfg}))
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(yaml_path))
    return yaml_path


def _dump_yaml(obj: dict[str, object]) -> str:
    """Hand-roll YAML so the test file stays stdlib-only."""
    import yaml

    return yaml.safe_dump(obj)


def _seed_priors(tmp_path, n: int) -> None:
    base = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        rp.append_sample(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            run_id=f"r{i}",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso=base.isoformat(),
            queue_wait_sec=120,
        )


class TestColdStart:
    def test_grows_mem_by_cluster_buffer(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch, cold_start_mem_buffer=0.15)
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"mem_mb": 16_000, "walltime_sec": 14400},
        )
        assert out.cold_start is True
        assert out.overrides["mem_mb"] == 18_400  # 16000 * 1.15
        assert "cold-start" in out.rationales["mem_mb"]

    def test_arbitrages_walltime_when_no_prior(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch, walltime_arbitrage=True)
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"walltime_sec": 14400},
        )
        # arbitrage_walltime(14400) → 13500 (15min subtracted, floor to 5min)
        assert out.overrides["walltime_sec"] == 13500
        assert "arbitrage" in out.rationales["walltime_sec"]

    def test_clamps_grown_mem_to_node_ceiling(self, tmp_path, monkeypatch):
        _write_clusters_yaml(
            tmp_path, monkeypatch, cold_start_mem_buffer=0.15, max_node_mem_mb=200_000
        )
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"mem_mb": 240_000},
        )
        assert out.overrides["mem_mb"] == 200_000
        assert "clamped" in out.rationales["mem_mb"]

    def test_skips_walltime_when_arbitrage_disabled(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch, walltime_arbitrage=False)
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"walltime_sec": 14400},
        )
        assert out.overrides["walltime_sec"] == 14400
        assert "walltime_sec" not in out.rationales

    def test_skips_mem_when_buffer_is_zero(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch, cold_start_mem_buffer=0.0)
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"mem_mb": 16_000},
        )
        assert out.overrides["mem_mb"] == 16_000
        assert "mem_mb" not in out.rationales


class TestWarmPath:
    def test_no_op_when_prior_is_warm(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed_priors(tmp_path, MIN_PRIOR_SAMPLES + 2)
        base = {"mem_mb": 16_000, "walltime_sec": 14400}
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides=base,
        )
        assert out.cold_start is False
        assert out.overrides == base
        assert out.rationales == {}


class TestDaisyChain:
    def test_flags_chain_required_when_walltime_exceeds_max(self, tmp_path, monkeypatch):
        # Cluster max 24h, ask 30h → chain required (with the 1h buffer).
        _write_clusters_yaml(
            tmp_path,
            monkeypatch,
            walltime_arbitrage=False,  # don't trim the ask
            max_walltime_sec=86400,
            auto_daisy_chain=True,
        )
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"walltime_sec": 108000},  # 30h
        )
        assert out.daisy_chain_required is True
        assert "daisy_chain" in out.rationales

    def test_no_chain_flag_when_within_ceiling(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch, max_walltime_sec=86400, auto_daisy_chain=True)
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"walltime_sec": 14400},
        )
        assert out.daisy_chain_required is False


class TestEdgeCases:
    def test_unknown_cluster_returns_overrides_unmodified(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        base = {"mem_mb": 16_000, "walltime_sec": 14400}
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster="nonexistent_cluster",
            base_overrides=base,
        )
        assert out.overrides == base
        assert out.cold_start is False
        assert out.rationales == {}

    def test_none_base_overrides_treated_as_empty(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides=None,
        )
        assert out.overrides == {}
        assert out.cold_start is True

    def test_overrides_pass_through_unknown_keys(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"mem_mb": 16_000, "gpus": 2, "constraint": "a100"},
        )
        assert out.overrides["gpus"] == 2
        assert out.overrides["constraint"] == "a100"

    def test_to_dict_serializes(self, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        out = plan_resubmit_overrides(
            tmp_path,
            profile=PROFILE,
            cluster=CLUSTER,
            base_overrides={"mem_mb": 16_000},
        )
        d = out.to_dict()
        assert "overrides" in d
        assert "rationales" in d
        assert "cold_start" in d
        assert "daisy_chain_required" in d


def test_yaml_dep_available():
    """Sanity: the test fixture relies on PyYAML being importable."""
    pytest.importorskip("yaml")
