"""Tests for hpc_agent_pro.planning.planner — integration via mocked snapshot."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from hpc_agent.infra import inspect as ins
from hpc_agent.infra.inspect import ClusterSnapshot, NodeSnapshot
from hpc_agent.state import runtime_prior as rp

from hpc_agent_pro.planning import planner


@pytest.fixture(autouse=True)
def _clear_inspect_cache():
    """Drop the module-global inspect cache between tests.

    The planner consults ``inspect_cluster`` whose 60s in-process cache
    would otherwise let one test's snapshot leak into the next.
    """
    ins._CACHE.clear()
    yield
    ins._CACHE.clear()


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
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,  # keep tests fast (no SSH probes)
            )
        assert out["needs_canary"] is True
        assert out["canary_plan"] is not None
        assert out["canary_plan"]["constraint"] == "a100"

    def test_candidates_include_default_pair(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(tmp_path, profile="x", cluster="discovery", adversarial=False)
        constraints = [c["constraint"] for c in out["candidates"]]
        # Default behavior: each gpu type + the union.
        assert "a100" in constraints
        assert "v100" in constraints
        assert "a100|v100" in constraints

    def test_stressed_node_surfaces_co_tenants(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="x",
                cluster="discovery",
                candidates=["v100"],
                adversarial=False,
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
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
            )
        assert out["needs_canary"] is False
        c = out["candidates"][0]
        assert c["runtime_prior_quantiles_sec"]["a100"]["n_samples"] == 5


class TestBuildCanaryPlan:
    def test_picks_lowest_eta_when_known(self):
        candidates = [
            {"constraint": "a100", "eta_sec_via_test_only": 600},
            {"constraint": "v100", "eta_sec_via_test_only": 100},
        ]
        plan = planner._build_canary_plan(candidates, profile="p", cluster="c")
        assert plan["constraint"] == "v100"

    def test_handles_all_unknown_etas(self):
        # When the scheduler returns no ETA for any candidate (typical on
        # SGE clusters where --test-only doesn't apply), the planner must
        # still pick a constraint deterministically rather than raising.
        candidates = [
            {"constraint": "a100", "eta_sec_via_test_only": None},
            {"constraint": "v100", "eta_sec_via_test_only": None},
        ]
        plan = planner._build_canary_plan(candidates, profile="p", cluster="c")
        # Stable sort preserves the input order; the first candidate wins.
        assert plan["constraint"] == "a100"
        assert plan["task_count"] == 1

    def test_empty_candidate_list_returns_note(self):
        plan = planner._build_canary_plan([], profile="p", cluster="c")
        assert plan["constraint"] is None
        assert "no candidates" in plan["note"]


class TestNodesForConstraint:
    def test_substring_overmatch_avoided(self):
        # `a10` must not match a node whose Gres advertises `a100`. The
        # naive substring-in approach would silently include this node;
        # the token-aware match correctly excludes it.
        from hpc_agent.infra.inspect import NodeSnapshot

        a100_node = NodeSnapshot(name="d11-07", gres="gpu:a100:2", active_features=["a100"])
        out = planner._nodes_for_constraint([a100_node], gpu_types=["a10"])
        assert out == []

    def test_exact_match_still_works(self):
        from hpc_agent.infra.inspect import NodeSnapshot

        a100_node = NodeSnapshot(name="d11-07", gres="gpu:a100:2", active_features=["a100"])
        out = planner._nodes_for_constraint([a100_node], gpu_types=["a100"])
        assert out == [a100_node]

    def test_active_features_fallback(self):
        # Some clusters expose the GPU type as a feature, not a GRES type.
        from hpc_agent.infra.inspect import NodeSnapshot

        node = NodeSnapshot(name="d11-08", gres="gpu:1", active_features=["v100"])
        out = planner._nodes_for_constraint([node], gpu_types=["v100"])
        assert out == [node]


class TestTestOnlyEtaParser:
    def test_parses_iso_timestamp(self):
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
        text = f"sbatch: Job 12345 to start at {future} using 1 ..."
        eta = planner._parse_test_only_eta(text)
        assert eta is not None
        assert 0 < eta < 700

    def test_unparseable_returns_none(self):
        assert planner._parse_test_only_eta("") is None
        assert planner._parse_test_only_eta("submission failed") is None


class TestAdversarialPath:
    """The default-on adversarial flow: walltime right-sizing + lattice probe."""

    @staticmethod
    def _canned_test_only(walltime_sec: int) -> str:
        """Synthesize a `sbatch --test-only` line whose ETA is short for
        small walltime asks and long for large ones — i.e., the smaller
        ask wins, modeling realistic backfill behavior.
        """
        from datetime import datetime, timedelta, timezone

        # Larger walltime ⇒ later predicted start.
        eta_min = max(1, walltime_sec // 60)
        future = (datetime.now(timezone.utc) + timedelta(minutes=eta_min)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        return f"sbatch: Job 1 to start at {future} using 1 ..."

    def test_recommended_tuple_picks_smallest_walltime(self, tmp_path, monkeypatch):
        from hpc_agent_pro.forecast import backfill as bf

        bf.clear_probe_cache()
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        # Seed enough priors to clear the n_samples >= 5 floor.
        for tid in range(8):
            rp.append_sample(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                run_id="r1",
                task_id=tid,
                gpu_type="a100",
                node="d11-07",
                elapsed_sec=1000,  # p95 = 1000s; ×1.30 = 1300s right-size
            )

        captured: list[int] = []

        def fake_probe(scheduler, cluster_cfg, *, constraint, walltime_sec, mem_mb, cpus):
            captured.append(walltime_sec)
            return planner._parse_test_only_eta(self._canned_test_only(walltime_sec)), ""

        with (
            patch(
                "hpc_agent_pro.planning.planner.inspect_cluster",
                return_value=_fake_snapshot(),
            ),  # noqa: E501
            patch(
                "hpc_agent_pro.planning.planner._eta_via_test_only_with_resources",
                side_effect=fake_probe,
            ),
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                candidates=["a100"],
                # adversarial=True is the default
            )

        c = out["candidates"][0]
        assert "recommended_tuple" in c, "adversarial path must populate recommended_tuple"
        assert "backfill_probes" in c
        # The 1.0× multiplier (1300s ≈ p95 × 1.30) wins because the canned
        # probe maps smaller walltime → smaller ETA.
        assert c["recommended_tuple"]["walltime_sec"] == 1300
        assert c["recommended_tuple"]["predicted_eta_sec"] is not None
        # Three lattice probes: 1.0×, 1.5×, 2.0× the right-sized walltime.
        assert {p["walltime_sec"] for p in c["backfill_probes"]} == {1300, 1950, 2600}
        # The legacy `eta_sec_via_test_only` field still probes once at 60s
        # for backward compat; assert the lattice asks are the new additions.
        adversarial_calls = [w for w in captured if w != 60]
        assert sorted(adversarial_calls) == [1300, 1950, 2600]

    def test_falls_back_when_no_priors(self, tmp_path, monkeypatch):
        from hpc_agent_pro.forecast import backfill as bf

        bf.clear_probe_cache()
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))

        def fake_probe(scheduler, cluster_cfg, *, constraint, walltime_sec, mem_mb, cpus):
            return None, ""  # every probe fails

        with (
            patch(
                "hpc_agent_pro.planning.planner.inspect_cluster",
                return_value=_fake_snapshot(),
            ),  # noqa: E501
            patch(
                "hpc_agent_pro.planning.planner._eta_via_test_only_with_resources",
                side_effect=fake_probe,
            ),
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                candidates=["a100"],
            )
        # No priors and no probe ETAs → recommended_tuple still surfaced
        # with the rationale, but predicted_eta_sec is None so the slash
        # command's auto-pick rule will skip and fall back.
        c = out["candidates"][0]
        rec = c["recommended_tuple"]
        assert rec["predicted_eta_sec"] is None
        assert "no usable prior" in rec["rationale"]
        assert out["needs_canary"] is True  # still triggers the canary path


# ---------------------------------------------------------------------------
# PR-C: cold-start walltime arbitrage
# ---------------------------------------------------------------------------


class TestColdStartWalltimeArbitrage:
    """The cold-start fallback: trim the user's nominal ask when the
    lattice probe has no priors to score against. The trim fits the
    campus user's job in backfill shadows the round-number ask doesn't
    reach.
    """

    def test_arbitrage_fires_when_no_priors_and_cluster_opted_in(self, tmp_path, monkeypatch):
        # No priors → no lattice pick → cold-start path → trim.
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
                walltime_user_ask_sec=14400,  # 4h nominal
            )
        # 4h trims to 3:45 (13500s); the response carries the original.
        assert out["walltime_arbitraged_from"] == 14400

    def test_arbitrage_does_not_fire_when_priors_pin_a_winner(self, tmp_path, monkeypatch):
        # Priors exist AND lattice probe returns a real ETA → the
        # lattice path supersedes arbitrage; we do NOT trim.
        from hpc_agent_pro.forecast import backfill as bf

        bf.clear_probe_cache()
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        for tid in range(8):
            rp.append_sample(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                run_id="r1",
                task_id=tid,
                gpu_type="a100",
                node="d11-07",
                elapsed_sec=1000,
            )

        from datetime import datetime, timedelta, timezone

        def fake_probe(scheduler, cluster_cfg, *, constraint, walltime_sec, mem_mb, cpus):
            future = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            return planner._parse_test_only_eta(
                f"sbatch: Job 1 to start at {future} using 1 ..."
            ), ""

        with (
            patch(
                "hpc_agent_pro.planning.planner.inspect_cluster",
                return_value=_fake_snapshot(),
            ),  # noqa: E501
            patch(
                "hpc_agent_pro.planning.planner._eta_via_test_only_with_resources",
                side_effect=fake_probe,
            ),
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                candidates=["a100"],
                walltime_user_ask_sec=14400,
            )
        assert out["walltime_arbitraged_from"] is None

    def test_arbitrage_disabled_per_cluster(self, tmp_path, monkeypatch):
        # Cluster opts out via walltime_arbitrage: false → no trim
        # even in cold-start.
        cfg_path = tmp_path / "clusters.yaml"
        cfg_path.write_text(
            "discovery:\n"
            "  host: example.invalid\n"
            "  user: tester\n"
            "  scheduler: slurm\n"
            "  scratch: /tmp\n"
            "  gpu_types: [a100]\n"
            "  walltime_arbitrage: false\n"
        )
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg_path))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="x",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
                walltime_user_ask_sec=14400,
            )
        assert out["walltime_arbitraged_from"] is None

    def test_arbitrage_null_when_no_user_ask(self, tmp_path, monkeypatch):
        # walltime_user_ask_sec=None → no arbitrage attempted, field is null.
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="x",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
            )
        assert out["walltime_arbitraged_from"] is None

    def test_arbitrage_null_when_below_floor(self, tmp_path, monkeypatch):
        # Sub-1h asks pass through the helper unchanged; the planner
        # records walltime_arbitraged_from=null because the trim is a
        # no-op (we only set the field when the trim actually fires).
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="x",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
                walltime_user_ask_sec=1800,  # 30min, below 1h floor
            )
        assert out["walltime_arbitraged_from"] is None


# ---------------------------------------------------------------------------
# Phase 4f: DES ETA layered alongside test-only
# ---------------------------------------------------------------------------


class TestEtaViaDES:
    def test_returns_none_without_snapshot_or_profiles(self, tmp_path):
        from hpc_agent_pro.planning.planner import _eta_via_des

        # Empty experiment dir → no DES inputs.
        assert _eta_via_des(tmp_path, "ml_ridge", "discovery") is None

    def test_returns_int_when_des_eligible(self, tmp_path):
        # Persist an idle snapshot — DES runs and returns 0.
        from hpc_agent.infra.inspect import (
            ClusterSnapshot,
            NodeSnapshot,
            persist_snapshot,
        )

        from hpc_agent_pro.planning.planner import _eta_via_des

        snap = ClusterSnapshot(
            cluster="discovery",
            scheduler_kind="slurm",
            now_iso="2026-04-28T10:00:00+00:00",
            nodes=[
                NodeSnapshot(
                    name="n0",
                    state="IDLE",
                    real_mem_mb=64_000,
                    alloc_mem_mb=0,
                    cpu_tot=8,
                    cpu_alloc=0,
                    gres="",
                    gres_used="",
                    co_tenants=[],
                    is_drained=False,
                )
            ],
        )
        persist_snapshot(tmp_path, snap)
        eta = _eta_via_des(tmp_path, "ml_ridge", "discovery")
        # Idle snapshot + small candidate → 0 wait.
        assert eta == 0
