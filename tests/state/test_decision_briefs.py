"""Tests for the decision-briefs state layer (``state/decision_briefs.py``).

The brief-side mirror of the decision journal (conduct rule 9, the
provenance gate — docs/design/history/proving-run-2-hardening.md §6). Covers the
append→read round-trip (order preserved), append-only discipline, the
on-disk JSONL locality, latest-per-block lookup with short-block-name
matching, and the fail-open-on-absence read.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent.state.decision_briefs import (
    append_brief,
    block_names_match,
    briefs_path,
    latest_brief_for_block,
    read_briefs,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_append_then_read_round_trip_preserves_order(tmp_path: Path) -> None:
    cases: list[tuple[str, dict[str, Any]]] = [
        ("s1", {"resolved": {"cluster": "hoffman2"}}),
        ("s2", {"est_core_hours": 40.0}),
    ]
    for block, brief in cases:
        append_brief(tmp_path, run_id="run-1", block=block, brief=brief)

    records = read_briefs(tmp_path, "run-1")
    assert [(r["block"], r["brief"]) for r in records] == [
        ("s1", {"resolved": {"cluster": "hoffman2"}}),
        ("s2", {"est_core_hours": 40.0}),
    ]
    for r in records:
        assert set(r) >= {"schema_version", "ts", "run_id", "block", "brief"}
        assert r["run_id"] == "run-1"


def test_append_is_append_only(tmp_path: Path) -> None:
    first = append_brief(tmp_path, run_id="run-x", block="s1", brief={"a": 1})
    append_brief(tmp_path, run_id="run-x", block="s1", brief={"a": 2})
    records = read_briefs(tmp_path, "run-x")
    assert len(records) == 2
    assert records[0] == first  # the first line is byte-preserved


def test_disk_format_is_one_json_object_per_line(tmp_path: Path) -> None:
    append_brief(tmp_path, run_id="run-y", block="s1", brief={"x": 1})
    append_brief(tmp_path, run_id="run-y", block="s2", brief={"y": 2})
    path = briefs_path(tmp_path, "run-y")
    assert path == tmp_path / ".hpc" / "runs" / "run-y.briefs.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_read_missing_journal_returns_empty(tmp_path: Path) -> None:
    # Fail-open signal the provenance gate relies on.
    assert read_briefs(tmp_path, "never-written") == []
    assert latest_brief_for_block(tmp_path, "never-written", "s1") is None


def test_latest_brief_for_block_returns_most_recent(tmp_path: Path) -> None:
    append_brief(tmp_path, run_id="r", block="s1", brief={"v": "old"})
    append_brief(tmp_path, run_id="r", block="s2", brief={"v": "other"})
    append_brief(tmp_path, run_id="r", block="s1", brief={"v": "new"})
    latest = latest_brief_for_block(tmp_path, "r", "s1")
    assert latest is not None
    assert latest["brief"] == {"v": "new"}


def test_latest_brief_for_block_matches_short_and_long_names(tmp_path: Path) -> None:
    # Persisted under the short form; queried with the long form (and vice versa).
    append_brief(tmp_path, run_id="r", block="s1", brief={"k": 1})
    assert latest_brief_for_block(tmp_path, "r", "submit-s1") is not None
    append_brief(tmp_path, run_id="r2", block="submit-s2", brief={"k": 2})
    assert latest_brief_for_block(tmp_path, "r2", "s2") is not None


def test_block_names_match_semantics() -> None:
    assert block_names_match("s1", "submit-s1")
    assert block_names_match("submit-s1", "s1")
    assert block_names_match("s1", "s1")
    assert not block_names_match("s1", "submit-s11")
    assert not block_names_match("s1", "s2")
    assert not block_names_match("", "s1")


def test_append_rejects_bad_run_id(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        append_brief(tmp_path, run_id="../escape", block="s1", brief={})
    with pytest.raises(errors.SpecInvalid):
        append_brief(tmp_path, run_id="r", block="", brief={})
