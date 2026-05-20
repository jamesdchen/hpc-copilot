"""Tests for the cold-start memory buffer plumbing (PR-B survival defense).

Covers two seams:

1. ``hpc_agent.infra.clusters.get_cold_start_mem_buffer`` — schema
   parser for the new ``cold_start_mem_buffer`` field in clusters.yaml,
   and the symmetric ``get_nfs_data_dir`` helper for the optional NFS
   staging path.
2. End-to-end through ``recommend_mem_mb`` from a synthetic cluster
   config dict, mirroring how the planner reads the field at submit
   time.

The "why": campus users on Hoffman2/CARC see their first run on a new
``(profile, cluster, cmd_sha)`` get bumped by the OOM daemon mid-write
because we have no prior to right-size against. The cold-start buffer
is a small, configurable headroom that shrinks back to zero once the
quantile-based prior takes over (≥5 samples per GPU type).
"""

from __future__ import annotations

import pytest

from hpc_agent.forecast.backfill import recommend_mem_mb
from hpc_agent.infra.clusters import get_cold_start_mem_buffer, get_nfs_data_dir

# ─── get_cold_start_mem_buffer schema ──────────────────────────────────────


class TestGetColdStartMemBuffer:
    def test_default_when_field_absent(self):
        """Empty cluster cfg → 15% default (the documented per-cluster baseline)."""
        assert get_cold_start_mem_buffer({}) == pytest.approx(0.15)

    def test_explicit_value_parses(self):
        """A cluster admin can override the default per-cluster."""
        cfg = {"cold_start_mem_buffer": 0.20}
        assert get_cold_start_mem_buffer(cfg) == pytest.approx(0.20)

    def test_zero_is_legal(self):
        """Zero opts out — legacy "kept user default" behavior."""
        cfg = {"cold_start_mem_buffer": 0.0}
        assert get_cold_start_mem_buffer(cfg) == 0.0

    def test_string_number_coerced(self):
        """YAML may parse some numeric forms as strings; coerce gracefully."""
        cfg = {"cold_start_mem_buffer": "0.10"}
        assert get_cold_start_mem_buffer(cfg) == pytest.approx(0.10)

    def test_negative_rejected(self):
        """Negative would *shrink* the ask — not a survival headroom."""
        with pytest.raises(ValueError, match="non-negative"):
            get_cold_start_mem_buffer({"cold_start_mem_buffer": -0.1})

    def test_non_numeric_rejected(self):
        with pytest.raises(ValueError, match="must be a number"):
            get_cold_start_mem_buffer({"cold_start_mem_buffer": "lots"})

    def test_explicit_default_kwarg(self):
        """Caller can pass a different baseline (e.g. for per-profile overrides)."""
        assert get_cold_start_mem_buffer({}, default=0.25) == pytest.approx(0.25)


# ─── get_nfs_data_dir schema ────────────────────────────────────────────────


class TestGetNfsDataDir:
    def test_unset_returns_none(self):
        """Most clusters won't have an NFS dataset path — None means "no staging"."""
        assert get_nfs_data_dir({}) is None

    def test_set_returns_string(self):
        cfg = {"nfs_data_dir": "/u/scratch/jdoe/datasets"}
        assert get_nfs_data_dir(cfg) == "/u/scratch/jdoe/datasets"

    def test_empty_string_rejected(self):
        """Empty string is a configuration error, not "disabled"."""
        with pytest.raises(ValueError, match="non-empty string"):
            get_nfs_data_dir({"nfs_data_dir": ""})

    def test_non_string_rejected(self):
        with pytest.raises(ValueError, match="non-empty string"):
            get_nfs_data_dir({"nfs_data_dir": ["/path"]})


# ─── end-to-end: cluster cfg → recommend_mem_mb ────────────────────────────


class TestEndToEndColdStart:
    """Mirror the planner's call shape: read the buffer from cluster cfg
    and pass it to ``recommend_mem_mb``. This guards against drift where
    the helper and the consumer diverge on type/units."""

    def test_no_prior_with_cluster_default(self):
        """Default 15% per the bundled clusters.yaml."""
        cfg = {"cold_start_mem_buffer": 0.15}
        buffer = get_cold_start_mem_buffer(cfg)
        mb, _ = recommend_mem_mb({}, ["a100"], user_default_mb=16384, cold_start_buffer=buffer)
        # 16384 * 1.15 = 18841.6 → 18842
        assert mb == 18842

    def test_no_prior_with_overridden_buffer(self):
        """A cluster with cold_start_mem_buffer: 0.20 grows 16 GB → 19.2 GB."""
        cfg = {"cold_start_mem_buffer": 0.20}
        buffer = get_cold_start_mem_buffer(cfg)
        mb, _ = recommend_mem_mb({}, ["a100"], user_default_mb=16384, cold_start_buffer=buffer)
        # 16384 * 1.20 = 19660.8 → 19661
        assert mb == 19661

    def test_priors_present_buffer_ignored(self):
        """Once priors exist, quantile-based shrink owns; buffer is inert."""
        cfg = {"cold_start_mem_buffer": 0.50}  # large buffer
        buffer = get_cold_start_mem_buffer(cfg)
        priors = {"a100": {"p95": 4096, "n_samples": 50}}
        mb, _ = recommend_mem_mb(
            priors,
            ["a100"],
            user_default_mb=16384,
            safety_mult=1.5,
            cold_start_buffer=buffer,
        )
        # Quantile path: 4096 * 1.5 = 6144 (well below user default)
        assert mb == 6144

    def test_zero_buffer_preserves_legacy(self):
        """Cluster with cold_start_mem_buffer: 0.0 keeps user default exactly."""
        cfg = {"cold_start_mem_buffer": 0.0}
        buffer = get_cold_start_mem_buffer(cfg)
        mb, rationale = recommend_mem_mb(
            {}, ["a100"], user_default_mb=16384, cold_start_buffer=buffer
        )
        assert mb == 16384
        assert "kept user default" in rationale


# ─── B-M5: per-node memory cap (max_node_mem_mb) ───────────────────────────


class TestMaxNodeMemClamp:
    """A user-asked --mem grown by the cold-start buffer can exceed the
    largest node the cluster will schedule (e.g. 240GB ask × 1.15
    buffer = 276GB on a 256GB node). Without a clamp the run sits
    Pending forever with ReqNodeNotAvail; the clamp pulls it back to
    a schedulable size and surfaces the rationale."""

    def test_buffer_within_ceiling_unaffected(self):
        """If grown ask ≤ ceiling, no clamp message."""
        mb, rationale = recommend_mem_mb(
            {},
            ["a100"],
            user_default_mb=100_000,
            cold_start_buffer=0.15,
            ceiling_mb=384_000,
        )
        # 100000 * 1.15 = 115000
        assert mb == 115_000
        assert "per-node cap" not in rationale

    def test_buffer_exceeds_ceiling_clamped(self):
        """240GB ask × 1.15 = 276GB clamped to 256GB node ceiling."""
        mb, rationale = recommend_mem_mb(
            {},
            ["a100"],
            user_default_mb=240_000,
            cold_start_buffer=0.15,
            ceiling_mb=256_000,
        )
        assert mb == 256_000, mb
        assert "per-node cap" in rationale
        assert "276000MB" in rationale, rationale  # surfaced the pre-clamp value

    def test_ceiling_unset_preserves_legacy(self):
        """ceiling_mb=None (default) leaves the buffer ungated — legacy."""
        mb, _ = recommend_mem_mb(
            {},
            ["a100"],
            user_default_mb=240_000,
            cold_start_buffer=0.15,
        )
        # No clamp; full grown value returned.
        assert mb == 276_000

    def test_user_default_above_ceiling_clamped_even_without_buffer(self):
        """A user_default already above the cap (no buffer) still
        gets clamped — otherwise the same ReqNodeNotAvail footgun
        bites a user with priors-exist=False but ceiling exceeded."""
        mb, rationale = recommend_mem_mb(
            {},
            ["a100"],
            user_default_mb=300_000,
            cold_start_buffer=0.0,
            ceiling_mb=256_000,
        )
        assert mb == 256_000, mb
        assert "per-node cap" in rationale

    def test_priors_path_also_clamps(self):
        """Even when priors drive the recommendation, the per-node cap
        applies — a misconfigured user_default + tiny prior shouldn't
        bypass the clamp."""
        priors = {"a100": {"p95": 400_000, "n_samples": 50}}
        mb, rationale = recommend_mem_mb(
            priors,
            ["a100"],
            user_default_mb=500_000,
            ceiling_mb=256_000,
        )
        assert mb == 256_000
        assert "per-node cap" in rationale

    def test_negative_ceiling_rejected(self):
        with pytest.raises(ValueError, match="ceiling_mb must be positive"):
            recommend_mem_mb({}, ["a100"], user_default_mb=16384, ceiling_mb=-1)


# ─── samples-cutoff transition + bundled-yaml round-trip ───────────────────


class TestNSamplesCutoff:
    """At the ``min_samples`` boundary, the recommender must transition
    cleanly between the cold-start path (no usable prior) and the
    quantile-based shrink path (priors usable)."""

    def test_below_min_samples_falls_back_to_buffer(self):
        """n_samples = min_samples - 1 → cold-start path applies."""
        priors = {"a100": {"p95": 4096, "n_samples": 9}}  # min_samples=10
        mb, rationale = recommend_mem_mb(
            priors,
            ["a100"],
            user_default_mb=16384,
            cold_start_buffer=0.15,
            min_samples=10,
        )
        assert mb == 18842  # 16384 * 1.15
        assert "cold-start buffer" in rationale

    def test_at_min_samples_uses_quantile_path(self):
        """n_samples = min_samples → quantile path takes over."""
        priors = {"a100": {"p95": 4096, "n_samples": 10}}
        mb, rationale = recommend_mem_mb(
            priors,
            ["a100"],
            user_default_mb=16384,
            cold_start_buffer=0.15,
            min_samples=10,
        )
        assert mb == 6144  # 4096 * 1.5
        # Quantile path's rationale, not cold-start.
        assert "p95×" in rationale


class TestBundledClustersYamlRoundTrip:
    """The bundled clusters.yaml must round-trip through every survival
    validator without raising. Catches a copy-paste bug (string where
    int expected, etc.) before it lands on a campus user's cluster."""

    def test_bundled_yaml_round_trips(self):
        from hpc_agent.infra.clusters import (
            get_auto_daisy_chain,
            get_max_node_mem_mb,
            get_max_walltime_sec,
            get_walltime_arbitrage,
            load_clusters_config,
        )

        clusters = load_clusters_config()
        assert clusters, "bundled clusters.yaml should define at least one cluster"
        for name, cfg in clusters.items():
            # Each call below would raise on a malformed value; the
            # assertion is implicit (no exception → contract holds).
            buf = get_cold_start_mem_buffer(cfg)
            assert buf >= 0.0, f"{name}: cold_start_mem_buffer < 0"
            _ = get_nfs_data_dir(cfg)  # may be None
            _ = get_walltime_arbitrage(cfg)  # bool
            _ = get_max_walltime_sec(cfg)  # positive int
            _ = get_auto_daisy_chain(cfg)  # bool|None
            ceiling = get_max_node_mem_mb(cfg)
            if ceiling is not None:
                assert ceiling > 0, f"{name}: max_node_mem_mb non-positive"
