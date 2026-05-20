"""Tests for ``forecast.sshare_parser.parse_sshare``."""

from __future__ import annotations

import textwrap

# ruff: noqa: E501 — fixture lines reproduce verbatim cluster output
from hpc_agent.forecast.sshare_parser import parse_sshare


def test_round_trip_standard_sshare_output() -> None:
    text = textwrap.dedent(
        """
        Account|User|RawShares|NormShares|RawUsage|EffectvUsage|FairShare
        root||1|1.000000|0|0.000000|1.000000
        labA|alice|1|0.500000|10000|0.250000|0.823456
        labA|bob|1|0.500000|20000|0.500000|0.412300
        labB|carol|1|0.250000|5000|0.125000|0.901234
        """
    ).strip()
    parsed = parse_sshare(text)
    assert set(parsed) == {"alice", "bob", "carol"}
    assert abs(parsed["alice"] - 0.823456) < 1e-6
    assert abs(parsed["carol"] - 0.901234) < 1e-6


def test_aggregate_account_rows_dropped() -> None:
    """Rows with no User (e.g. ``root||...``) are aggregate-account
    summaries; we want per-user only."""
    text = textwrap.dedent(
        """
        Account|User|FairShare
        root||1.000000
        lab1||0.555555
        alice|alice|0.823456
        """
    ).strip()
    parsed = parse_sshare(text)
    assert set(parsed) == {"alice"}


def test_unparseable_fairshare_skipped() -> None:
    text = textwrap.dedent(
        """
        Account|User|FairShare
        a|alice|nope
        b|bob|0.5
        """
    ).strip()
    parsed = parse_sshare(text)
    assert set(parsed) == {"bob"}


def test_column_order_independent() -> None:
    text = textwrap.dedent(
        """
        FairShare|User|Account|RawUsage
        0.7|alice|labA|100
        """
    ).strip()
    parsed = parse_sshare(text)
    assert parsed == {"alice": 0.7}


def test_missing_user_column_returns_empty_dict() -> None:
    text = textwrap.dedent(
        """
        Account|FairShare
        labA|0.5
        """
    ).strip()
    assert parse_sshare(text) == {}


def test_missing_fairshare_column_returns_empty_dict() -> None:
    text = textwrap.dedent(
        """
        Account|User|RawUsage
        labA|alice|100
        """
    ).strip()
    assert parse_sshare(text) == {}


def test_empty_input_returns_empty_dict() -> None:
    assert parse_sshare("") == {}
    assert parse_sshare("\n\n") == {}


def test_header_only_returns_empty_dict() -> None:
    assert parse_sshare("Account|User|FairShare\n") == {}
