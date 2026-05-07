"""Tests for ``scripts.extract_sacct_history.parse_sacct_lines``.

Pure parser; tests use synthetic sacct output.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

# ruff: noqa: E501

_SPEC = importlib.util.spec_from_file_location(
    "_extract_sacct_history",
    Path(__file__).resolve().parent.parent.parent / "scripts" / "extract_sacct_history.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_parses_standard_sacct_output() -> None:
    text = textwrap.dedent(
        """
        JobID|Submit|Start|Priority|Partition|User|TimeLimit
        12345|2026-09-22T10:00:00|2026-09-22T10:30:00|1234|gpu|alice|04:00:00
        """
    ).strip()
    rows = _MOD.parse_sacct_lines(text)
    assert len(rows) == 1
    r = rows[0]
    assert r["job_id"] == "12345"
    assert r["submit_iso"] == "2026-09-22T10:00:00+00:00"
    assert r["start_iso"] == "2026-09-22T10:30:00+00:00"
    assert r["priority"] == 1234
    assert r["partition"] == "gpu"
    assert r["user"] == "alice"
    assert r["walltime_sec"] == 4 * 3600


def test_skips_step_rows() -> None:
    """``12345.batch`` etc. are step rows — skip; only top-level jobs."""
    text = textwrap.dedent(
        """
        JobID|Submit|Start|Priority|Partition|User|TimeLimit
        12345|2026-09-22T10:00:00|2026-09-22T10:30:00|1234|gpu|alice|04:00:00
        12345.batch|2026-09-22T10:00:00|2026-09-22T10:30:00|1234|gpu|alice|04:00:00
        12345.0|2026-09-22T10:00:00|2026-09-22T10:30:00|1234|gpu|alice|04:00:00
        """
    ).strip()
    rows = _MOD.parse_sacct_lines(text)
    assert [r["job_id"] for r in rows] == ["12345"]


def test_skips_jobs_without_start_time() -> None:
    """Pending / cancelled-before-start jobs have no Start; skip."""
    text = textwrap.dedent(
        """
        JobID|Submit|Start|Priority|Partition|User|TimeLimit
        99999|2026-09-22T10:00:00|Unknown|1234|gpu|alice|04:00:00
        """
    ).strip()
    assert _MOD.parse_sacct_lines(text) == []


def test_skips_jobs_with_unparseable_walltime() -> None:
    text = textwrap.dedent(
        """
        JobID|Submit|Start|Priority|Partition|User|TimeLimit
        1|2026-09-22T10:00:00|2026-09-22T10:30:00|1234|gpu|alice|garbage
        """
    ).strip()
    assert _MOD.parse_sacct_lines(text) == []


def test_handles_unlimited_timelimit_as_skip() -> None:
    """``UNLIMITED`` walltime means we don't have a walltime label —
    skip these rows for training (we can't build a meaningful
    ``walltime_sec`` feature)."""
    text = textwrap.dedent(
        """
        JobID|Submit|Start|Priority|Partition|User|TimeLimit
        1|2026-09-22T10:00:00|2026-09-22T10:30:00|1234|gpu|alice|UNLIMITED
        """
    ).strip()
    assert _MOD.parse_sacct_lines(text) == []


def test_parses_dhms_walltime_format() -> None:
    """``2-04:00:00`` = 2 days 4h."""
    text = textwrap.dedent(
        """
        JobID|Submit|Start|Priority|Partition|User|TimeLimit
        1|2026-09-22T10:00:00|2026-09-22T10:30:00|1234|gpu|alice|2-04:00:00
        """
    ).strip()
    rows = _MOD.parse_sacct_lines(text)
    assert rows[0]["walltime_sec"] == 2 * 86400 + 4 * 3600


def test_empty_input_returns_empty_list() -> None:
    assert _MOD.parse_sacct_lines("") == []
    assert _MOD.parse_sacct_lines("\n\n") == []
