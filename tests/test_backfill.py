"""Tests for hpc_mapreduce.job.backfill — right-sizing + lattice probing."""

from __future__ import annotations

import pytest

from hpc_mapreduce.job import backfill as bf


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    bf.clear_probe_cache()
    yield
    bf.clear_probe_cache()


# ─── recommend_walltime_sec ────────────────────────────────────────────────


class TestRecommendWalltime:
    def test_no_quantiles_returns_fallback_with_rationale(self):
        wt, rationale = bf.recommend_walltime_sec({}, ["a100"], fallback_sec=4 * 3600)
        assert wt == 4 * 3600
        assert "no usable prior" in rationale

    def test_below_min_samples_falls_back(self):
        # 3 samples isn't enough; we must not trust the prior.
        q = {"a100": {"p50": 1000, "p95": 1500, "p99": 1700, "n_samples": 3}}
        wt, rationale = bf.recommend_walltime_sec(
            q, ["a100"], fallback_sec=999, min_samples=5
        )
        assert wt == 999
        assert "no usable prior" in rationale

    def test_picks_worst_p95_across_constraint_pool(self):
        # Constraint "a100|v100" should size for the slowest GPU type
        # because the scheduler may land us on either.
        q = {
            "a100": {"p50": 1000, "p95": 1200, "n_samples": 10},
            "v100": {"p50": 2000, "p95": 2400, "n_samples": 10},
        }
        wt, rationale = bf.recommend_walltime_sec(q, ["a100", "v100"], safety_mult=1.0)
        assert wt == 2400
        assert "v100" in rationale

    def test_safety_multiplier_applied(self):
        q = {"a100": {"p50": 1000, "p95": 1000, "n_samples": 10}}
        wt, _ = bf.recommend_walltime_sec(q, ["a100"], safety_mult=1.5, floor_sec=0)
        assert wt == 1500

    def test_floor_clamp(self):
        q = {"a100": {"p50": 50, "p95": 60, "n_samples": 10}}
        wt, rationale = bf.recommend_walltime_sec(
            q, ["a100"], safety_mult=1.0, floor_sec=600
        )
        assert wt == 600
        assert "clamped" in rationale

    def test_ceiling_clamp(self):
        q = {"a100": {"p50": 100000, "p95": 100000, "n_samples": 10}}
        wt, rationale = bf.recommend_walltime_sec(
            q, ["a100"], safety_mult=1.0, ceiling_sec=3600
        )
        assert wt == 3600
        assert "clamped" in rationale

    def test_rejects_zero_p95(self):
        # A degenerate prior (p95=0 from a corrupt sample) must not be
        # trusted — pretend there are no usable samples.
        q = {"a100": {"p50": 0, "p95": 0, "n_samples": 50}}
        wt, rationale = bf.recommend_walltime_sec(q, ["a100"], fallback_sec=999)
        assert wt == 999
        assert "no usable prior" in rationale

    def test_invalid_safety_mult_raises(self):
        with pytest.raises(ValueError):
            bf.recommend_walltime_sec({}, ["a100"], safety_mult=0)


# ─── build_lattice ─────────────────────────────────────────────────────────


class TestBuildLattice:
    def test_default_three_point(self):
        base = bf.ResourceTuple(constraint="a100", walltime_sec=600)
        out = bf.build_lattice(base)
        assert [t.walltime_sec for t in out] == [600, 900, 1200]
        assert all(t.constraint == "a100" for t in out)

    def test_ceiling_collapses_redundant_multipliers(self):
        # 1.0× = 600, 1.5× = 900 (clamped to 800), 2.0× = 1200 (clamped to 800)
        # → dedup yields [600, 800], not three 800s.
        base = bf.ResourceTuple(constraint="a100", walltime_sec=600)
        out = bf.build_lattice(base, walltime_ceiling_sec=800)
        assert [t.walltime_sec for t in out] == [600, 800]

    def test_empty_multipliers_falls_back_to_base(self):
        base = bf.ResourceTuple(constraint="a100", walltime_sec=600)
        out = bf.build_lattice(base, walltime_multipliers=())
        assert out == [base]


# ─── probe_lattice ─────────────────────────────────────────────────────────


class TestProbeLattice:
    def test_preserves_order_under_threadpool(self):
        # The threadpool returns futures in completion order, but the
        # contract is that out[i] corresponds to lattice[i]. Use a probe
        # that's deliberately slow on the first input to force out-of-order
        # completion, then assert the returned list is still index-aligned.
        import time as _time

        lattice = [
            bf.ResourceTuple(constraint=f"c{i}", walltime_sec=100 + i) for i in range(4)
        ]

        def probe(t: bf.ResourceTuple) -> bf.BackfillProbe:
            if t.constraint == "c0":
                _time.sleep(0.05)
            return bf.BackfillProbe(tuple_=t, eta_sec=t.walltime_sec, raw_test_only="")

        out = bf.probe_lattice(lattice, probe, max_parallel=4)
        assert [p.tuple_.constraint for p in out] == ["c0", "c1", "c2", "c3"]

    def test_empty_lattice_returns_empty(self):
        assert bf.probe_lattice([], lambda t: None) == []  # type: ignore[arg-type]

    def test_serial_path_when_one_worker(self):
        lattice = [bf.ResourceTuple(constraint="a100", walltime_sec=600)]
        calls: list[bf.ResourceTuple] = []

        def probe(t: bf.ResourceTuple) -> bf.BackfillProbe:
            calls.append(t)
            return bf.BackfillProbe(tuple_=t, eta_sec=42, raw_test_only="")

        out = bf.probe_lattice(lattice, probe, max_parallel=1)
        assert len(out) == 1
        assert out[0].eta_sec == 42
        assert calls == lattice


# ─── pick_earliest ─────────────────────────────────────────────────────────


class TestPickEarliest:
    def test_picks_smallest_eta(self):
        probes = [
            bf.BackfillProbe(
                tuple_=bf.ResourceTuple(constraint="a100", walltime_sec=600),
                eta_sec=300,
                raw_test_only="",
            ),
            bf.BackfillProbe(
                tuple_=bf.ResourceTuple(constraint="a100", walltime_sec=900),
                eta_sec=120,
                raw_test_only="",
            ),
        ]
        pick = bf.pick_earliest(probes)
        assert pick is not None
        assert pick.eta_sec == 120

    def test_tie_break_prefers_smaller_walltime(self):
        # Two tuples predicted to start at the same time — prefer the
        # tighter ask so we leave more cluster headroom for the next gap.
        probes = [
            bf.BackfillProbe(
                tuple_=bf.ResourceTuple(constraint="a100", walltime_sec=900),
                eta_sec=200,
                raw_test_only="",
            ),
            bf.BackfillProbe(
                tuple_=bf.ResourceTuple(constraint="a100", walltime_sec=600),
                eta_sec=200,
                raw_test_only="",
            ),
        ]
        pick = bf.pick_earliest(probes)
        assert pick is not None
        assert pick.tuple_.walltime_sec == 600

    def test_all_none_returns_none(self):
        probes = [
            bf.BackfillProbe(
                tuple_=bf.ResourceTuple(constraint="a100", walltime_sec=600),
                eta_sec=None,
                raw_test_only="",
            )
        ]
        assert bf.pick_earliest(probes) is None


# ─── cached_probe ──────────────────────────────────────────────────────────


class TestCachedProbe:
    def test_second_call_hits_cache(self):
        calls = 0

        def probe(t: bf.ResourceTuple) -> bf.BackfillProbe:
            nonlocal calls
            calls += 1
            return bf.BackfillProbe(tuple_=t, eta_sec=42, raw_test_only="")

        wrapped = bf.cached_probe("discovery", probe)
        t = bf.ResourceTuple(constraint="a100", walltime_sec=600)
        wrapped(t)
        wrapped(t)
        assert calls == 1

    def test_different_walltime_bucket_reprobes(self):
        # Walltimes 600s (bucket 10) and 720s (bucket 12) hit different cache
        # keys even on the same constraint.
        calls = 0

        def probe(t: bf.ResourceTuple) -> bf.BackfillProbe:
            nonlocal calls
            calls += 1
            return bf.BackfillProbe(tuple_=t, eta_sec=42, raw_test_only="")

        wrapped = bf.cached_probe("discovery", probe)
        wrapped(bf.ResourceTuple(constraint="a100", walltime_sec=600))
        wrapped(bf.ResourceTuple(constraint="a100", walltime_sec=720))
        assert calls == 2
