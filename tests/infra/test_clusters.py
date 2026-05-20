"""Tests for the per-cluster validator helpers in
:mod:`hpc_agent.infra.clusters` for the PR-C survival-defense knobs.

Each helper applies a default and rejects wrong-typed yaml values so
e.g. ``walltime_arbitrage: "yes"`` (a string) doesn't silently flip the
feature on/off — the bad value fails loudly at load time.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.clusters import (
    get_auto_daisy_chain,
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
        with pytest.raises(ValueError, match="walltime_arbitrage"):
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
        with pytest.raises(ValueError, match="auto_daisy_chain"):
            get_auto_daisy_chain({"auto_daisy_chain": bad})


# ─── get_max_walltime_sec ───────────────────────────────────────────────────


class TestGetMaxWalltimeSec:
    def test_default_24h(self):
        # Absent key -> 86400s (24h), a typical campus-cluster ceiling.
        assert get_max_walltime_sec({}) == 86400

    def test_explicit_value(self):
        assert get_max_walltime_sec({"max_walltime_sec": 172800}) == 172800

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="positive"):
            get_max_walltime_sec({"max_walltime_sec": 0})

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="positive"):
            get_max_walltime_sec({"max_walltime_sec": -1})

    @pytest.mark.parametrize("bad", ["86400", 86400.0, True, False, [86400], None])
    def test_rejects_non_int(self, bad):
        with pytest.raises(ValueError, match="max_walltime_sec"):
            get_max_walltime_sec({"max_walltime_sec": bad})
