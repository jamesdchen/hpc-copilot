"""Tests for ``forecast.academic_calendar``."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from hpc_agent_pro.forecast.academic_calendar import (
    Deadline,
    features_at,
    load_project_deadlines,
)

if TYPE_CHECKING:
    from pathlib import Path


_DEADLINES = (
    Deadline("ICLR-2027", "paper", "2026-09-25"),
    Deadline("NeurIPS-2026", "camera_ready", "2026-10-25"),
    Deadline("ICML-2027", "abstract", "2027-01-23"),
)


def _at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# ─── features_at ──────────────────────────────────────────────────────


def test_features_when_deadline_within_week() -> None:
    out = features_at(_at("2026-09-20T10:00:00"), deadlines=_DEADLINES)
    assert out["min_days_to_deadline"] == 5
    assert out["is_within_deadline_week"] is True
    assert out["is_within_deadline_month"] is True


def test_features_when_no_deadlines_within_a_month() -> None:
    """ICLR 9-25, next is Oct 25; check for a date 60d before ICLR."""
    out = features_at(_at("2026-07-01T10:00:00"), deadlines=_DEADLINES)
    assert out["min_days_to_deadline"] == 86  # to ICLR-2027 paper
    assert out["is_within_deadline_week"] is False
    assert out["is_within_deadline_month"] is False
    assert out["deadline_density_30d"] == 0


def test_density_counts_multiple_deadlines_in_window() -> None:
    """At Sep 26 (just past ICLR), NeurIPS camera-ready (Oct 25, 29
    days out) is in the 30d window. ICLR (Sep 25) is 1 day past — not
    counted. Pin density_30d == 1 here. Use a different query date for
    the multi-deadline case (Sep 27 puts both NeurIPS Oct 25 and ICLR
    if it were upcoming in window)."""
    # Use a date where TWO deadlines are within 30 days. Add a synthetic
    # second deadline 28d away from the query.
    deadlines = (
        Deadline("V1", "paper", "2026-10-15"),  # 28 days from Sep 17
        Deadline("V2", "paper", "2026-10-20"),  # 33 days — not in window
        Deadline("V3", "paper", "2026-10-10"),  # 23 days — in window
    )
    out = features_at(_at("2026-09-17T10:00:00"), deadlines=deadlines)
    # V1 (28d) + V3 (23d) within 30d; V2 (33d) excluded.
    assert out["deadline_density_30d"] == 2


def test_past_deadlines_ignored() -> None:
    """When all calendar deadlines are in the past, every feature
    collapses to "no upcoming"."""
    out = features_at(_at("2099-01-01T00:00:00"), deadlines=_DEADLINES)
    assert out["min_days_to_deadline"] is None
    assert out["is_within_deadline_week"] is False
    assert out["deadline_density_30d"] == 0


def test_features_with_default_calendar_returns_dict() -> None:
    """Smoke test on the shipped DEFAULT_DEADLINES — at minimum the
    schema is correct."""
    out = features_at(_at("2026-09-22T10:00:00"))
    assert "min_days_to_deadline" in out
    assert "deadline_density_30d" in out


# ─── load_project_deadlines ───────────────────────────────────────────


def test_missing_deadlines_yaml_returns_default(tmp_path: Path) -> None:
    out = load_project_deadlines(tmp_path)
    assert out  # non-empty default


def test_well_formed_deadlines_yaml_round_trips(tmp_path: Path) -> None:
    target = tmp_path / ".hpc" / "deadlines.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        """
        deadlines:
          - {venue: NLP-Workshop, kind: paper, date: 2027-03-15}
          - {venue: my-internal-deadline, kind: review, date: 2027-04-01}
        """
    )
    out = load_project_deadlines(tmp_path)
    assert {d.venue for d in out} == {"NLP-Workshop", "my-internal-deadline"}


def test_malformed_deadlines_yaml_raises(tmp_path: Path) -> None:
    target = tmp_path / ".hpc" / "deadlines.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not yaml at all")
    with pytest.raises(ValueError, match="parse error"):
        load_project_deadlines(tmp_path)


def test_deadlines_yaml_missing_required_key_raises(tmp_path: Path) -> None:
    target = tmp_path / ".hpc" / "deadlines.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("deadlines:\n  - {venue: x, kind: paper}\n")
    with pytest.raises(ValueError, match="missing key 'date'"):
        load_project_deadlines(tmp_path)
