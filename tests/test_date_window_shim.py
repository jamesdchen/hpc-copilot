"""Tests for templates/date_window_shim.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SHIM_PATH = Path(__file__).parent.parent / "templates" / "date_window_shim.py"


def _load_shim():
    """Load the shim module by path (it lives under templates/, not a package)."""
    spec = importlib.util.spec_from_file_location("date_window_shim", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def shim():
    return _load_shim()


def test_periods_6M_over_5_years(shim):
    """Default constants: (2020-01-01, 2024-12-31, 6M) -> 10 half-year periods."""
    periods = shim._periods()
    assert len(periods) == 10
    assert periods[0] == ("2020-01-01", "2020-06-30")
    assert periods[-1] == ("2024-07-01", "2024-12-31")


def test_periods_30m_sub_daily(shim, monkeypatch):
    """Sub-daily suffix 'm' produces 30-minute datetime windows clamped to END.

    The ported (historical) ``while cursor <= overall_end`` walker is
    inclusive of ``END``, so a half-open intent of 4x30min windows over
    ``[00:00, 02:00)`` is expressed with ``END=01:59:59``.
    """
    monkeypatch.setattr(shim, "START", "2020-01-01T00:00:00")
    monkeypatch.setattr(shim, "END", "2020-01-01T01:59:59")
    monkeypatch.setattr(shim, "CHUNK_DUR", "30m")

    periods = shim._periods()
    assert len(periods) == 4
    assert periods[0] == ("2020-01-01T00:00:00", "2020-01-01T00:29:59")
    assert periods[1] == ("2020-01-01T00:30:00", "2020-01-01T00:59:59")
    assert periods[2] == ("2020-01-01T01:00:00", "2020-01-01T01:29:59")
    assert periods[3] == ("2020-01-01T01:30:00", "2020-01-01T01:59:59")


def test_periods_year_duration(shim, monkeypatch):
    """1Y suffix exercises the ``_add_months(amount * 12)`` branch."""
    monkeypatch.setattr(shim, "START", "2020-01-01")
    monkeypatch.setattr(shim, "END", "2022-12-31")
    monkeypatch.setattr(shim, "CHUNK_DUR", "1Y")

    periods = shim._periods()
    assert len(periods) == 3
    assert periods[0] == ("2020-01-01", "2020-12-31")
    assert periods[1] == ("2021-01-01", "2021-12-31")
    assert periods[2] == ("2022-01-01", "2022-12-31")


def test_periods_single_period(shim, monkeypatch):
    """CHUNK_DUR >= full span collapses to one period clamped at END."""
    monkeypatch.setattr(shim, "START", "2020-01-01")
    monkeypatch.setattr(shim, "END", "2020-12-31")
    monkeypatch.setattr(shim, "CHUNK_DUR", "5Y")

    periods = shim._periods()
    assert len(periods) == 1
    assert periods[0] == ("2020-01-01", "2020-12-31")


def test_translate_returns_start_end_pair(shim):
    """translate(0, total) returns [START_ARG, start_iso, END_ARG, end_iso]."""
    periods = shim._periods()
    result = shim.translate(0, len(periods))
    assert result == [shim.START_ARG, periods[0][0], shim.END_ARG, periods[0][1]]


def test_translate_total_chunks_mismatch_raises(shim):
    """A mismatched total_chunks must fail loudly with both counts in the message."""
    expected = len(shim._periods())
    with pytest.raises(AssertionError) as excinfo:
        shim.translate(0, 999)
    msg = str(excinfo.value)
    assert "999" in msg
    assert str(expected) in msg


def test_add_months_preserves_tzinfo_and_microsecond(shim):
    """_add_months must round-trip tzinfo and sub-second precision.

    Stripping either silently corrupts schedules whose START is timezone-aware
    or has microsecond resolution: subsequent comparisons mix naive/aware
    datetimes (TypeError) or drift by microseconds across every step.
    """
    from datetime import datetime, timezone

    src = datetime(2020, 1, 31, 12, 30, 45, 123456, tzinfo=timezone.utc)
    out = shim._add_months(src, 1)
    assert out.tzinfo is timezone.utc
    assert out.microsecond == 123456
    assert (out.year, out.month, out.day) == (2020, 2, 29)
    assert (out.hour, out.minute, out.second) == (12, 30, 45)


def test_translate_respects_custom_arg_names(shim, monkeypatch):
    """Custom START_ARG/END_ARG propagate into the returned list."""
    monkeypatch.setattr(shim, "START_ARG", "--from")
    monkeypatch.setattr(shim, "END_ARG", "--to")

    periods = shim._periods()
    result = shim.translate(0, len(periods))
    assert result[0] == "--from"
    assert result[2] == "--to"
    assert result[1] == periods[0][0]
    assert result[3] == periods[0][1]
