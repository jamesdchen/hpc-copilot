"""Tests for :func:`hpc_agent_pro.atoms.walltime_arbitrage.arbitrage_walltime`.

The helper is a pure function over an integer ask. We pin every boundary
case so the cold-start fallback shape (``- 15min, floor to 5min``) cannot
silently drift.
"""

from __future__ import annotations

import pytest

from hpc_agent_pro.atoms.walltime_arbitrage import arbitrage_walltime


class TestArbitrageWalltime:
    def test_below_floor_returns_unchanged(self):
        # Anything strictly below 1h passes through untouched — short asks
        # don't sit in backfill long enough for the trim to pay off.
        assert arbitrage_walltime(0) == 0
        assert arbitrage_walltime(60) == 60
        assert arbitrage_walltime(1800) == 1800
        assert arbitrage_walltime(3599) == 3599

    def test_one_hour_boundary(self):
        # 3600s -> (3600-900)//300*300 == 2700 (45min). The first ask we
        # actually arbitrage.
        assert arbitrage_walltime(3600) == 2700

    def test_four_hours(self):
        # 4:00:00 -> 3:45:00. Headline survival case.
        assert arbitrage_walltime(14400) == 13500

    def test_eight_hours(self):
        # 8:00:00 -> 7:45:00.
        assert arbitrage_walltime(28800) == 27900

    def test_twenty_four_hours(self):
        # 24:00:00 -> 23:45:00. Common Hoffman2 hard ceiling.
        assert arbitrage_walltime(86400) == 85500

    @pytest.mark.parametrize(
        "ask_sec, expected_sec",
        [
            (3600, 2700),
            (7200, 6300),  # 2h -> 1:45
            (14400, 13500),  # 4h -> 3:45
            (28800, 27900),  # 8h -> 7:45
            (43200, 42300),  # 12h -> 11:45
            (86400, 85500),  # 24h -> 23:45
            (172800, 171900),  # 48h -> 47:45
        ],
    )
    def test_table(self, ask_sec, expected_sec):
        assert arbitrage_walltime(ask_sec) == expected_sec

    def test_result_is_strictly_less_when_arbitraged(self):
        # When arbitrage fires (ask >= floor), the result is always
        # strictly less than the input — that's the whole point.
        for ask in (3600, 5000, 7200, 14400, 86400):
            assert arbitrage_walltime(ask) < ask

    def test_result_is_5min_aligned_when_arbitraged(self):
        for ask in (3600, 5000, 7200, 14400, 86400):
            assert arbitrage_walltime(ask) % 300 == 0
