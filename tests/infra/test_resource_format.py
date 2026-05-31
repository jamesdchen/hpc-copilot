"""Tests for the canonical resource value-coercion helper (#206).

``infra/resource_format.py`` consolidates the clamp / ceil / walltime
formatting that used to live in three places. These tests pin the
edge-case behaviour the consolidation has to preserve byte-for-byte:

* :func:`walltime_hms` must reproduce both the old ``sge._fmt_hms`` and
  ``recover_flow._format_walltime`` outputs (they agreed for all
  non-negative inputs; the new one keeps the SGE ``max(0, ...)`` guard).
* :func:`coerce` must implement None-passthrough, ``math.ceil``, clamp,
  and ``fmt="time"`` delegation in the documented order.

The matching backend / recover-flow tests (``test_backends_sge.py``,
``test_backends_slurm.py``, ``test_flow_cluster.py``) still pin the
through-the-scheduler shape; these focus on the helper in isolation.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.resource_format import coerce, walltime_hms


class TestWalltimeHms:
    """Boundary table for the single canonical sec→HH:MM:SS formatter."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "00:00:00"),  # zero floor — two-digit everywhere
            (1, "00:00:01"),
            (59, "00:00:59"),  # last second before a minute rolls
            (60, "00:01:00"),  # minute boundary
            (61, "00:01:01"),
            (3599, "00:59:59"),  # last second before an hour rolls
            (3600, "01:00:00"),  # hour boundary
            (3661, "01:01:01"),  # the classic 1h1m1s
            (7200, "02:00:00"),  # pins test_backends_sge: 2h on the dot
            (14400, "04:00:00"),  # pins test_flow_cluster: 4h
            (65, "00:01:05"),  # pins test_flow_cluster: 65s → 00:01:05
            (90061, "25:01:01"),  # >24h: hours past two digits, no wrap
            (360000, "100:00:00"),  # >=100h: 3-digit hours render intact
            (86399, "23:59:59"),  # one second under a day
            (86400, "24:00:00"),  # exactly a day
        ],
    )
    def test_boundaries(self, seconds: int, expected: str) -> None:
        assert walltime_hms(seconds) == expected

    def test_negative_clamps_to_zero(self) -> None:
        # Mirrors the old sge._fmt_hms ``max(0, ...)`` guard; a negative
        # walltime would otherwise emit a ``-`` the scheduler rejects.
        assert walltime_hms(-1) == "00:00:00"
        assert walltime_hms(-3600) == "00:00:00"

    def test_float_truncates_through_int(self) -> None:
        # ``int()`` truncates toward zero (matches the historical
        # ``int(total_seconds)`` cast); use coerce(ceil=True) upstream for
        # round-up semantics.
        assert walltime_hms(3661.9) == "01:01:01"  # type: ignore[arg-type]

    def test_matches_naive_divmod_reference(self) -> None:
        # Cross-check against an independent implementation across a wide
        # range so a future "optimisation" of the formatter can't silently
        # change a single value.
        for sec in (0, 7, 119, 3600, 7384, 100_000, 999_999):
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            assert walltime_hms(sec) == f"{h:02d}:{m:02d}:{s:02d}"


class TestCoerceNonePassthrough:
    def test_none_returns_none_regardless_of_other_args(self) -> None:
        # The "omit the optional directive" contract: a None value is
        # never clamped, ceiled, or formatted into a default.
        assert coerce(None) is None
        assert coerce(None, minimum=1, maximum=10, ceil=True) is None
        assert coerce(None, fmt="time") is None


class TestCoerceCeil:
    def test_ceil_rounds_up_fractional(self) -> None:
        # No fractional cores/nodes — 1.2 → 2.
        assert coerce(1.2, ceil=True) == 2
        assert coerce(0.1, ceil=True) == 1

    def test_ceil_exact_integer_unchanged(self) -> None:
        assert coerce(4.0, ceil=True) == 4
        assert coerce(4, ceil=True) == 4

    def test_no_ceil_leaves_value_untouched(self) -> None:
        assert coerce(1.2) == 1.2

    def test_slurm_minutes_ceil_division_equivalence(self) -> None:
        # The SLURM ``--time`` path computes ``coerce(sec/60, ceil=True)``.
        # Verify it equals the old ``-(-sec // 60)`` ceil-division idiom.
        for sec in (1, 59, 60, 61, 90, 119, 120, 7200):
            assert coerce(sec / 60, ceil=True) == -(-sec // 60)


class TestCoerceClamp:
    def test_maximum_caps(self) -> None:
        assert coerce(50, maximum=10) == 10
        assert coerce(5, maximum=10) == 5

    def test_minimum_floors(self) -> None:
        assert coerce(0, minimum=1) == 1
        assert coerce(5, minimum=1) == 5

    def test_both_bounds(self) -> None:
        assert coerce(50, minimum=1, maximum=10) == 10
        assert coerce(0, minimum=1, maximum=10) == 1
        assert coerce(5, minimum=1, maximum=10) == 5

    def test_none_bound_leaves_side_unconstrained(self) -> None:
        assert coerce(1_000_000, minimum=1, maximum=None) == 1_000_000
        assert coerce(-50, minimum=None, maximum=10) == -50

    def test_contradictory_bounds_minimum_wins(self) -> None:
        # maximum is applied first, then minimum, so a min above the max
        # floors the result to the min (documented precedence).
        assert coerce(5, minimum=100, maximum=10) == 100

    def test_ceil_then_clamp_order(self) -> None:
        # ceil precedes clamp: 9.1 → ceil 10 → clamp to max 10 (kept, not
        # pushed over and clamped back). Mirrors the throughput per-batch
        # ``coerce(total/n_batches, maximum=max_array_size, ceil=True)``.
        assert coerce(9.1, maximum=10, ceil=True) == 10
        assert coerce(10.4, maximum=10, ceil=True) == 10  # ceil→11, clamp→10


class TestCoerceFormat:
    def test_fmt_time_delegates_to_walltime_hms(self) -> None:
        assert coerce(3661, fmt="time") == "01:01:01"
        assert coerce(7200, fmt="time") == "02:00:00"

    def test_fmt_time_with_ceil_rounds_before_formatting(self) -> None:
        # ceil runs first, so a fractional second is rounded up, then
        # formatted — distinct from walltime_hms's bare truncation.
        assert coerce(3661.1, ceil=True, fmt="time") == "01:01:02"

    def test_fmt_time_with_clamp(self) -> None:
        # Clamp the seconds to a cluster ceiling, then format.
        assert coerce(100_000, maximum=86_400, fmt="time") == "24:00:00"

    def test_unknown_fmt_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="unknown fmt"):
            coerce(10, fmt="bogus")
