"""request_id replay dedup on the decision journal (unit D-FSYNC, Δ2b).

The state-layer ``append_decision`` gains a client-minted ``request_id``: a
same-id re-append is a **replay no-op** (nothing written, the original record
returned), closing the run-#2 duplicate-greenlight class. ``request_id=None``
stays byte-identical (no stamp, ordinary append).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent.state.decision_journal import append_decision, decisions_path, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

_SCOPE = ("run", "20260101-000000-deadbee")


def _raw(tmp_path: Path) -> list[dict]:
    path = decisions_path(tmp_path, *_SCOPE)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _append(tmp_path: Path, *, request_id: str | None, response: str = "y") -> dict:
    return append_decision(
        tmp_path,
        scope_kind=_SCOPE[0],
        scope_id=_SCOPE[1],
        block="submit.S1",
        response=response,
        request_id=request_id,
    )


def test_same_request_id_twice_one_record(tmp_path: Path) -> None:
    """Same request_id twice -> ONE record; the replay returns the original."""
    first = _append(tmp_path, request_id="req-1", response="y")
    replay = _append(tmp_path, request_id="req-1", response="different-nudge")

    # The replay returns the ORIGINAL record (first response), writes nothing.
    assert replay["response"] == "y"
    assert replay == first
    records = read_decisions(tmp_path, *_SCOPE)
    assert len(records) == 1
    assert records[0]["request_id"] == "req-1"


def test_different_request_ids_two_records(tmp_path: Path) -> None:
    """Different request_ids -> two records."""
    _append(tmp_path, request_id="req-1")
    _append(tmp_path, request_id="req-2")
    records = read_decisions(tmp_path, *_SCOPE)
    assert [r["request_id"] for r in records] == ["req-1", "req-2"]


def test_no_request_id_is_byte_identical(tmp_path: Path) -> None:
    """request_id=None -> no request_id key on disk; two appends land two lines
    (byte-identical to pre-Δ2b append-only)."""
    _append(tmp_path, request_id=None)
    _append(tmp_path, request_id=None)
    records = _raw(tmp_path)
    assert len(records) == 2
    for rec in records:
        assert "request_id" not in rec


def test_dedup_scoped_per_journal_id_reuse_across_scopes(tmp_path: Path) -> None:
    """A request_id dedups only within its own journal file — the same id used in
    two different run scopes is not cross-deduped (ids are minted per call)."""
    append_decision(
        tmp_path, scope_kind="run", scope_id="run-a", block="b", response="y", request_id="shared"
    )
    append_decision(
        tmp_path, scope_kind="run", scope_id="run-b", block="b", response="y", request_id="shared"
    )
    assert len(read_decisions(tmp_path, "run", "run-a")) == 1
    assert len(read_decisions(tmp_path, "run", "run-b")) == 1
