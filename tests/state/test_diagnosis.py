"""Tests for the agent-diagnosis sidecar store (park-time diagnosis seam).

The store's whole contract is trust-shaped: OVERWRITE-on-reattach (advisory,
newest wins), provenance stamped by the WRITER (never caller-supplied — a
provenance the agent could set itself is no guard), fail-open reads (absent /
corrupt / unprovenanced → ``None``), and pointer+count projections for the
surfaces (never the content).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.state.diagnosis import (
    AGENT_AUTHOR,
    diagnosis_path,
    diagnosis_pointer,
    diagnosis_pointer_line,
    read_diagnosis,
    write_diagnosis,
)

_EXCERPT = {"path": "/logs/task.err", "lines": "CUDA out of memory"}
_ACTION = {
    "label": "raise mem",
    "rationale": "gpu_oom signature",
    "suggested_response_text": "bump mem_mb 1.5x and resubmit",
}


def test_roundtrip_and_provenance_stamp(tmp_path: Path) -> None:
    """Write → read returns the dossier, provenance-stamped agent by the WRITER."""
    record = write_diagnosis(
        tmp_path,
        run_id="run-1",
        classification="gpu_oom",
        evidence_excerpts=[_EXCERPT],
        proposed_actions=[_ACTION],
    )
    assert record["provenance"]["authored_by"] == AGENT_AUTHOR
    assert record["provenance"]["attached_at"]

    read = read_diagnosis(tmp_path, "run-1")
    assert read is not None
    assert read["classification"] == "gpu_oom"
    assert read["evidence_excerpts"] == [_EXCERPT]
    assert read["proposed_actions"] == [_ACTION]
    assert read["provenance"]["authored_by"] == AGENT_AUTHOR
    assert diagnosis_path(tmp_path, "run-1").is_file()


def test_reattach_overwrites_newest_wins(tmp_path: Path) -> None:
    """Re-attach OVERWRITES — one file, the newest advisory dossier."""
    write_diagnosis(
        tmp_path,
        run_id="run-1",
        classification="gpu_oom",
        evidence_excerpts=[],
        proposed_actions=[_ACTION],
    )
    write_diagnosis(
        tmp_path,
        run_id="run-1",
        classification="walltime",
        evidence_excerpts=[_EXCERPT],
        proposed_actions=[],
    )
    read = read_diagnosis(tmp_path, "run-1")
    assert read is not None
    assert read["classification"] == "walltime"
    assert read["proposed_actions"] == []
    # One file, not an accumulating history.
    files = list((tmp_path / ".hpc" / "runs").glob("run-1.diagnosis*"))
    assert [p.name for p in files if p.suffix == ".json"] == ["run-1.diagnosis.json"]


def test_read_fail_open_on_absent_and_corrupt(tmp_path: Path) -> None:
    assert read_diagnosis(tmp_path, "ghost") is None
    path = diagnosis_path(tmp_path, "torn")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert read_diagnosis(tmp_path, "torn") is None
    path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert read_diagnosis(tmp_path, "torn") is None


def test_read_refuses_an_unprovenanced_record(tmp_path: Path) -> None:
    """A record NOT stamped agent-authored reads as absent — the label guarantee:
    every dossier any surface points at is provenance-marked, because the reader
    refuses everything else (a hand-forged 'human' stamp included)."""
    path = diagnosis_path(tmp_path, "forged")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"classification": "gpu_oom", "provenance": {"authored_by": "human"}}),
        encoding="utf-8",
    )
    assert read_diagnosis(tmp_path, "forged") is None
    path.write_text(json.dumps({"classification": "gpu_oom"}), encoding="utf-8")
    assert read_diagnosis(tmp_path, "forged") is None


def test_writer_cannot_be_handed_a_provenance(tmp_path: Path) -> None:
    """The write seam simply has no provenance parameter — the affordance is
    absent, not prose-guarded."""
    with pytest.raises(TypeError):
        write_diagnosis(  # type: ignore[call-arg]
            tmp_path,
            run_id="run-1",
            classification="gpu_oom",
            evidence_excerpts=[],
            proposed_actions=[],
            provenance={"authored_by": "code"},
        )


def test_pointer_is_counts_and_path_only(tmp_path: Path) -> None:
    """The surface projection carries pointer + counts, never excerpts/actions."""
    assert diagnosis_pointer(tmp_path, "run-1") is None
    write_diagnosis(
        tmp_path,
        run_id="run-1",
        classification="gpu_oom",
        evidence_excerpts=[_EXCERPT],
        proposed_actions=[_ACTION, _ACTION],
    )
    pointer = diagnosis_pointer(tmp_path, "run-1")
    assert pointer is not None
    assert set(pointer) == {"path", "attached_at", "classification", "proposed_actions_count"}
    assert pointer["proposed_actions_count"] == 2
    assert pointer["path"].endswith("run-1.diagnosis.json")
    # Content never rides the pointer.
    assert "suggested_response_text" not in json.dumps(pointer)


def test_pointer_line_is_the_one_rendering(tmp_path: Path) -> None:
    assert diagnosis_pointer_line(None) == "none"
    write_diagnosis(
        tmp_path,
        run_id="run-1",
        classification="gpu_oom",
        evidence_excerpts=[],
        proposed_actions=[_ACTION],
    )
    line = diagnosis_pointer_line(diagnosis_pointer(tmp_path, "run-1"))
    assert line.startswith("attached (1 proposed action(s), agent-authored, advisory) — ")
    assert line.endswith("run-1.diagnosis.json")


def test_fs_unsafe_run_id_is_refused(tmp_path: Path) -> None:
    for bad in ("", "../escape", "a/b", "a\\b", "."):
        with pytest.raises(errors.SpecInvalid):
            diagnosis_path(tmp_path, bad)
