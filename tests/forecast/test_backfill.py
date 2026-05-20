"""Tests for hpc_agent.forecast.backfill — right-sizing + lattice probing."""

from __future__ import annotations

import pytest

from hpc_agent.forecast import backfill as bf


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
        wt, rationale = bf.recommend_walltime_sec(q, ["a100"], fallback_sec=999, min_samples=5)
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
        wt, rationale = bf.recommend_walltime_sec(q, ["a100"], safety_mult=1.0, floor_sec=600)
        assert wt == 600
        assert "clamped" in rationale

    def test_ceiling_clamp(self):
        q = {"a100": {"p50": 100000, "p95": 100000, "n_samples": 10}}
        wt, rationale = bf.recommend_walltime_sec(q, ["a100"], safety_mult=1.0, ceiling_sec=3600)
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

        lattice = [bf.ResourceTuple(constraint=f"c{i}", walltime_sec=100 + i) for i in range(4)]

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


# ─── footprint shrink: recommend_mem_mb / recommend_cpus ───────────────────


class TestRecommendMem:
    def test_no_prior_keeps_user_default(self):
        mb, rationale = bf.recommend_mem_mb({}, ["a100"], user_default_mb=16384)
        assert mb == 16384
        assert "no usable" in rationale

    def test_below_min_samples_keeps_default(self):
        q = {"a100": {"p95": 4096, "n_samples": 5}}
        mb, rationale = bf.recommend_mem_mb(q, ["a100"], user_default_mb=16384, min_samples=10)
        assert mb == 16384
        assert "no usable" in rationale

    def test_shrinks_when_prior_below_default(self):
        q = {"a100": {"p95": 4096, "n_samples": 50}}
        mb, rationale = bf.recommend_mem_mb(q, ["a100"], user_default_mb=16384, safety_mult=1.5)
        # 4096 * 1.5 = 6144, well under 16384.
        assert mb == 6144
        assert "was 16384MB" in rationale

    def test_never_grows_above_user_default(self):
        # The prior says we needed 20000MB (95th percentile of past
        # peak_host_mem_mb). User asked for 16384. We must not silently
        # bump them above their ask — only shrink. Surface the conflict.
        q = {"a100": {"p95": 20000, "n_samples": 50}}
        mb, rationale = bf.recommend_mem_mb(q, ["a100"], user_default_mb=16384)
        assert mb == 16384
        assert "keeping user default" in rationale

    def test_floor_clamp(self):
        q = {"a100": {"p95": 100, "n_samples": 50}}
        mb, _ = bf.recommend_mem_mb(
            q, ["a100"], user_default_mb=16384, floor_mb=512, safety_mult=1.0
        )
        assert mb == 512

    def test_picks_worst_p95_across_pool(self):
        q = {
            "a100": {"p95": 4000, "n_samples": 50},
            "v100": {"p95": 8000, "n_samples": 50},
        }
        mb, rationale = bf.recommend_mem_mb(
            q, ["a100", "v100"], user_default_mb=16384, safety_mult=1.0
        )
        assert mb == 8000
        assert "v100" in rationale


# ─── cold-start memory buffer (PR-B) ───────────────────────────────────────


class TestColdStartMemBuffer:
    """Cold-start (no prior) headroom against the OOM daemon.

    When no usable runtime prior exists, we grow the user's --mem ask
    by ``(1 + cold_start_buffer)`` so the OOM daemon doesn't bump the
    campus user's brand-new run mid-write. Once priors land per GPU
    type the quantile-based shrink takes over and the buffer is no
    longer applied.
    """

    def test_no_prior_default_buffer_is_zero(self):
        """Legacy callers (no buffer kwarg) keep ``user_default_mb`` exactly."""
        mb, rationale = bf.recommend_mem_mb({}, ["a100"], user_default_mb=16384)
        assert mb == 16384
        assert "no usable" in rationale
        assert "cold-start" not in rationale

    def test_no_prior_with_15pct_buffer_grows_ask(self):
        """16 GB × 1.15 = 18.4 GB (rounded to 18842 MB)."""
        mb, rationale = bf.recommend_mem_mb(
            {}, ["a100"], user_default_mb=16384, cold_start_buffer=0.15
        )
        # 16384 * 1.15 = 18841.6 → 18842
        assert mb == 18842
        assert "cold-start buffer" in rationale
        assert "OOM-daemon" in rationale

    def test_no_prior_with_20pct_buffer(self):
        """20% buffer: 16 GB × 1.20 = 19.2 GB."""
        mb, _ = bf.recommend_mem_mb({}, ["a100"], user_default_mb=16384, cold_start_buffer=0.20)
        # 16384 * 1.20 = 19660.8 → 19661
        assert mb == 19661

    def test_buffer_not_applied_when_prior_exists(self):
        """When priors exist the quantile shrink owns; buffer is ignored."""
        q = {"a100": {"p95": 4096, "n_samples": 50}}
        mb_with = bf.recommend_mem_mb(
            q, ["a100"], user_default_mb=16384, safety_mult=1.5, cold_start_buffer=0.50
        )[0]
        mb_without = bf.recommend_mem_mb(
            q, ["a100"], user_default_mb=16384, safety_mult=1.5, cold_start_buffer=0.0
        )[0]
        # Both paths shrink to 4096 * 1.5 = 6144; the cold-start buffer
        # MUST NOT inflate this — priors already encode the safety
        # margin via walltime-drift calibration.
        assert mb_with == mb_without == 6144

    def test_below_min_samples_uses_buffer(self):
        """A handful of samples isn't enough — still cold-start."""
        q = {"a100": {"p95": 4096, "n_samples": 5}}
        mb, rationale = bf.recommend_mem_mb(
            q,
            ["a100"],
            user_default_mb=16384,
            min_samples=10,
            cold_start_buffer=0.15,
        )
        assert mb == 18842
        assert "cold-start buffer" in rationale

    def test_negative_buffer_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            bf.recommend_mem_mb({}, ["a100"], user_default_mb=16384, cold_start_buffer=-0.1)

    def test_buffer_respects_floor(self):
        """Tiny user ask + buffer still floors at floor_mb."""
        mb, _ = bf.recommend_mem_mb(
            {}, ["a100"], user_default_mb=100, cold_start_buffer=0.15, floor_mb=512
        )
        assert mb == 512


class TestRecommendCpus:
    def test_no_prior_keeps_user_default(self):
        c, rationale = bf.recommend_cpus({}, ["a100"], user_default_cpus=4)
        assert c == 4
        assert "no usable" in rationale

    def test_shrinks_when_prior_below_default(self):
        # User asked 4 cores; prior shows we only used 2 cores at p95.
        # Recommend 2 + 1 (safety pad) = 3.
        q = {"a100": {"p95": 2, "n_samples": 50}}
        c, _ = bf.recommend_cpus(q, ["a100"], user_default_cpus=4)
        assert c == 3

    def test_floor_at_one_core(self):
        q = {"a100": {"p95": 0, "n_samples": 50}}  # zero filtered out → no usable
        c, rationale = bf.recommend_cpus(q, ["a100"], user_default_cpus=4)
        assert c == 4  # falls back to user default since p95 is 0
        assert "no usable" in rationale

    def test_never_grows(self):
        # Prior says we needed 16 cores; user asked 4. Don't bump.
        q = {"a100": {"p95": 16, "n_samples": 50}}
        c, _ = bf.recommend_cpus(q, ["a100"], user_default_cpus=4)
        assert c == 4


# ─── multi-dim lattice ────────────────────────────────────────────────────


class TestMultiDimLattice:
    def test_walltime_only_when_single_mem_mult(self):
        base = bf.ResourceTuple(constraint="a100", walltime_sec=600, mem_mb=4096)
        out = bf.build_lattice(base)
        # Default mem_multipliers=(1.0,) ⇒ 3 walltime points × 1 mem = 3 tuples.
        assert len(out) == 3
        assert all(t.mem_mb == 4096 for t in out)

    def test_walltime_x_mem_cross_product(self):
        base = bf.ResourceTuple(constraint="a100", walltime_sec=600, mem_mb=4096)
        out = bf.build_lattice(base, mem_multipliers=(1.0, 1.5))
        # 3 walltime × 2 mem = 6 tuples, all distinct (wt, mem) pairs.
        assert len(out) == 6
        pairs = {(t.walltime_sec, t.mem_mb) for t in out}
        assert len(pairs) == 6

    def test_max_probes_caps_lattice(self):
        base = bf.ResourceTuple(constraint="a100", walltime_sec=600, mem_mb=4096)
        out = bf.build_lattice(
            base,
            walltime_multipliers=(1.0, 1.25, 1.5, 1.75, 2.0),
            mem_multipliers=(1.0, 1.5, 2.0),
            max_probes=6,
        )
        assert len(out) == 6


# ─── array reshape ─────────────────────────────────────────────────────────


class TestArrayReshape:
    def test_no_inputs_falls_back_to_halving(self):
        new, rationale = bf.reshape_array_size_for_backfill(
            current_max_array_size=1000, target_window_sec=None, est_per_task_sec=None
        )
        assert new == 500
        assert "halving" in rationale

    def test_already_at_floor_returns_unchanged(self):
        new, rationale = bf.reshape_array_size_for_backfill(
            current_max_array_size=1, target_window_sec=None, est_per_task_sec=None
        )
        assert new == 1
        assert "floor" in rationale

    def test_window_aware_reshape(self):
        # Per-task ~3600s, target window ~1800s ⇒ ratio = 2 ⇒ shrink by 1+2=3.
        new, rationale = bf.reshape_array_size_for_backfill(
            current_max_array_size=900,
            target_window_sec=1800,
            est_per_task_sec=3600,
        )
        assert new == 300
        assert "1800s backfill window" in rationale

    def test_window_already_fits(self):
        # If per-task fits target window, no reshape — wasteful.
        new, rationale = bf.reshape_array_size_for_backfill(
            current_max_array_size=1000,
            target_window_sec=3600,
            est_per_task_sec=600,
        )
        assert new == 1000
        assert "no reshape" in rationale


# ─── walltime split (job splitting) ────────────────────────────────────────


class TestWalltimeSplit:
    def test_no_split_when_walltime_fits_window(self):
        seg = bf.split_walltime_into_segments(walltime_sec=1800, target_window_sec=3600)
        assert seg.n_segments == 1
        assert seg.requires_checkpointing is False
        assert "no split" in seg.rationale

    def test_splits_long_walltime(self):
        # 6h walltime, 30m target ⇒ ceil(21600/1800) = 12 ideal segments,
        # but the default max_segments=8 cap limits the chain to keep
        # dependency overhead manageable. Each segment is then sized so
        # n × seg ≥ walltime; here that's 8 × 2700s = 21600s.
        seg = bf.split_walltime_into_segments(walltime_sec=21600, target_window_sec=1800)
        assert seg.n_segments == 8  # capped by max_segments default
        assert seg.segment_walltime_sec == 2700  # 21600 / 8
        assert seg.requires_checkpointing is True
        assert "REQUIRES" in seg.rationale  # surface the checkpoint warning

    def test_uncapped_segment_count_when_max_high(self):
        # If you raise max_segments, the chain is allowed to grow to fit
        # the target window exactly — verify the cap is the only limiter.
        seg = bf.split_walltime_into_segments(
            walltime_sec=21600, target_window_sec=1800, max_segments=20
        )
        assert seg.n_segments == 12
        assert seg.segment_walltime_sec == 1800

    def test_max_segments_caps_chain(self):
        # 24h ÷ 30m = 48 ideal segments, but cap at max_segments=8.
        seg = bf.split_walltime_into_segments(
            walltime_sec=86400, target_window_sec=1800, max_segments=8
        )
        assert seg.n_segments == 8
        assert seg.segment_walltime_sec * seg.n_segments >= 86400

    def test_floor_segment_sec(self):
        # Tiny target window must not yield 30-second segments.
        seg = bf.split_walltime_into_segments(
            walltime_sec=3600, target_window_sec=10, floor_segment_sec=600
        )
        assert seg.segment_walltime_sec >= 600

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError):
            bf.split_walltime_into_segments(walltime_sec=0, target_window_sec=600)
        with pytest.raises(ValueError):
            bf.split_walltime_into_segments(walltime_sec=600, target_window_sec=0)
