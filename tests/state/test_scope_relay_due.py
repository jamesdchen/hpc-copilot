"""Scope-generalized relay-due markers (run-#10 #13) + the F-I tar dialects.

The omission gate's second source: campaign-run terminals arm markers on the
campaign journal; the Stop hook's discharge pass scans them alongside the
notebook audits. Fires-and-passes per the design's safety properties.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.state.notebook_audit import (
    read_undischarged_relay_markers,
    record_relay_discharge,
    record_scope_relay_due,
)


def _mark(exp: Path, stage: str = "submit_failed") -> dict | None:
    return record_scope_relay_due(
        exp,
        scope_kind="campaign",
        scope_id="camp1",
        record_kind="campaign-run",
        key_tokens=[stage, "run-abc123"],
    )


def test_campaign_marker_written_and_read(tmp_path: Path) -> None:
    rec = _mark(tmp_path)
    assert rec is not None
    markers = read_undischarged_relay_markers(tmp_path, "camp1", scope_kind="campaign")
    assert len(markers) == 1
    assert markers[0]["record_kind"] == "campaign-run"
    assert markers[0]["key_tokens"] == ["submit_failed", "run-abc123"]
    # The notebook scope does NOT see campaign markers (scope isolation).
    assert read_undischarged_relay_markers(tmp_path, "camp1") == []


def test_identical_marker_does_not_rearm(tmp_path: Path) -> None:
    assert _mark(tmp_path) is not None
    assert _mark(tmp_path) is None  # dedup on (record_kind, key_tokens)
    assert len(read_undischarged_relay_markers(tmp_path, "camp1", scope_kind="campaign")) == 1


def test_discharge_clears_campaign_marker(tmp_path: Path) -> None:
    _mark(tmp_path)
    [marker] = read_undischarged_relay_markers(tmp_path, "camp1", scope_kind="campaign")
    record_relay_discharge(tmp_path, audit_id="camp1", marker=marker, scope_kind="campaign")
    assert read_undischarged_relay_markers(tmp_path, "camp1", scope_kind="campaign") == []


def test_empty_tokens_write_nothing(tmp_path: Path) -> None:
    out = record_scope_relay_due(
        tmp_path, scope_kind="campaign", scope_id="camp1", record_kind="x", key_tokens=[]
    )
    assert out is None
    assert read_undischarged_relay_markers(tmp_path, "camp1", scope_kind="campaign") == []
