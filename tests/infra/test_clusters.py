"""Tests for the per-cluster validator helpers in
:mod:`hpc_agent.infra.clusters` for the PR-C survival-defense knobs.

Each helper applies a default and rejects wrong-typed yaml values so
e.g. ``walltime_arbitrage: "yes"`` (a string) doesn't silently flip the
feature on/off — the bad value fails loudly at load time.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra.clusters import (
    _COLD_START_WALLTIME_SEC,
    get_auto_daisy_chain,
    get_default_walltime_sec,
    get_max_walltime_sec,
    get_walltime_arbitrage,
)

# ─── get_walltime_arbitrage ─────────────────────────────────────────────────


class TestGetWalltimeArbitrage:
    def test_default_true(self):
        # Absent key -> default True (the helper is opt-out, not opt-in).
        assert get_walltime_arbitrage({}) is True

    def test_explicit_true(self):
        assert get_walltime_arbitrage({"walltime_arbitrage": True}) is True

    def test_explicit_false(self):
        assert get_walltime_arbitrage({"walltime_arbitrage": False}) is False

    @pytest.mark.parametrize(
        "bad",
        ["yes", "true", 1, 0, 1.0, [], {}, None],
    )
    def test_rejects_non_bool(self, bad):
        with pytest.raises(errors.SpecInvalid, match="walltime_arbitrage"):
            get_walltime_arbitrage({"walltime_arbitrage": bad})


# ─── get_auto_daisy_chain ───────────────────────────────────────────────────


class TestGetAutoDaisyChain:
    def test_absent_returns_none(self):
        # Absent key -> None ("use detection").
        assert get_auto_daisy_chain({}) is None

    def test_explicit_none_returns_none(self):
        # Explicit None -> None (same as absent).
        assert get_auto_daisy_chain({"auto_daisy_chain": None}) is None

    def test_explicit_true(self):
        # Always-chain override.
        assert get_auto_daisy_chain({"auto_daisy_chain": True}) is True

    def test_explicit_false(self):
        # Kill switch — never chain on this cluster.
        assert get_auto_daisy_chain({"auto_daisy_chain": False}) is False

    @pytest.mark.parametrize("bad", ["yes", "true", 1, 0, 1.0, []])
    def test_rejects_non_bool(self, bad):
        with pytest.raises(errors.SpecInvalid, match="auto_daisy_chain"):
            get_auto_daisy_chain({"auto_daisy_chain": bad})


# ─── get_max_walltime_sec ───────────────────────────────────────────────────


class TestGetMaxWalltimeSec:
    def test_default_24h(self):
        # Absent key -> 86400s (24h), a typical campus-cluster ceiling.
        assert get_max_walltime_sec({}) == 86400

    def test_explicit_value(self):
        assert get_max_walltime_sec({"max_walltime_sec": 172800}) == 172800

    def test_rejects_zero(self):
        with pytest.raises(errors.SpecInvalid, match="positive"):
            get_max_walltime_sec({"max_walltime_sec": 0})

    def test_rejects_negative(self):
        with pytest.raises(errors.SpecInvalid, match="positive"):
            get_max_walltime_sec({"max_walltime_sec": -1})

    @pytest.mark.parametrize("bad", ["86400", 86400.0, True, False, [86400], None])
    def test_rejects_non_int(self, bad):
        with pytest.raises(errors.SpecInvalid, match="max_walltime_sec"):
            get_max_walltime_sec({"max_walltime_sec": bad})


# ─── get_default_walltime_sec (cold-start fallback, #170) ────────────────────


class TestGetDefaultWalltimeSec:
    def test_absent_returns_conservative_floor(self):
        # No prior, no operator override, no optional prior-reading verb: the
        # fallback MUST still resolve (#170) to the conservative built-in floor.
        assert get_default_walltime_sec({}) == _COLD_START_WALLTIME_SEC

    def test_explicit_value_used(self):
        assert get_default_walltime_sec({"default_walltime_sec": 7200}) == 7200

    def test_floor_clamped_to_max_walltime(self):
        # A small-ceiling cluster never gets a cold-start ask above what its
        # scheduler accepts — the floor is clamped to max_walltime_sec.
        cfg = {"max_walltime_sec": 3600}
        assert get_default_walltime_sec(cfg) == 3600

    def test_explicit_value_clamped_to_max_walltime(self):
        cfg = {"default_walltime_sec": 999999, "max_walltime_sec": 7200}
        assert get_default_walltime_sec(cfg) == 7200

    def test_rejects_zero(self):
        with pytest.raises(errors.SpecInvalid, match="positive"):
            get_default_walltime_sec({"default_walltime_sec": 0})

    def test_rejects_negative(self):
        with pytest.raises(errors.SpecInvalid, match="positive"):
            get_default_walltime_sec({"default_walltime_sec": -1})

    @pytest.mark.parametrize("bad", ["7200", 7200.0, True, False, [7200]])
    def test_rejects_non_int(self, bad):
        # None is NOT in this list: an absent key is the valid cold-start path.
        with pytest.raises(errors.SpecInvalid, match="default_walltime_sec"):
            get_default_walltime_sec({"default_walltime_sec": bad})

    def test_demo_clusters_yaml_resolves_for_every_cluster(self):
        # The shipped clusters.yaml has no default_walltime_sec (issue #170's
        # second gap); the resolver must still produce a value for each stanza.
        from hpc_agent.infra.clusters import load_clusters_config

        clusters = load_clusters_config()
        assert clusters  # sanity: the packaged config loaded
        for name, cfg in clusters.items():
            wt = get_default_walltime_sec(cfg)
            assert wt > 0, name
            assert wt <= get_max_walltime_sec(cfg), name
