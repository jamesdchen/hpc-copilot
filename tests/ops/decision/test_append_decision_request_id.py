"""append-decision primitive: request_id replay dedup + fsync durability ack.

Unit D-FSYNC end-to-end at the primitive boundary:

* Δ2b — a client-minted ``request_id`` (sourced from ``provenance["request_id"]``
  on the frozen input spec) makes a re-append a replay no-op: same id twice ->
  one record; different ids -> two.
* Durability kill-pair — an ``fsync`` ``OSError`` on the source-of-truth decision
  journal PROPAGATES (no ack without durability); the primitive never returns a
  success result for a non-durable write.

The append shape is the byte-identity-floor campaign greenlight (block
``campaign-greenlight``, response ``y``) that passes every authorship gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.infra import io as io_mod
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from pathlib import Path

_SCOPE = ("campaign", "widget-camp")


def _greenlight(tmp_path: Path, *, request_id: str | None = None) -> Any:
    prov: dict[str, Any] = {}
    if request_id is not None:
        prov["request_id"] = request_id
    return append_decision(
        experiment_dir=tmp_path,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": _SCOPE[0],
                "scope_id": _SCOPE[1],
                "block": "campaign-greenlight",
                "response": "y",
                "resolved": {},
                "provenance": prov,
            }
        ),
    )


def test_same_request_id_twice_one_record(tmp_path: Path) -> None:
    first = _greenlight(tmp_path, request_id="rpc-1")
    replay = _greenlight(tmp_path, request_id="rpc-1")
    # Replay: the primitive re-surfaces the ORIGINAL record, count stays 1.
    assert replay.count == 1
    assert first.record.ts == replay.record.ts
    assert len(read_decisions(tmp_path, *_SCOPE)) == 1


def test_different_request_ids_two_records(tmp_path: Path) -> None:
    _greenlight(tmp_path, request_id="rpc-1")
    _greenlight(tmp_path, request_id="rpc-2")
    assert len(read_decisions(tmp_path, *_SCOPE)) == 2


def test_no_request_id_ordinary_append(tmp_path: Path) -> None:
    """No provenance request_id -> ordinary append-only (no dedup, no stamp)."""
    _greenlight(tmp_path)
    _greenlight(tmp_path)
    records = read_decisions(tmp_path, *_SCOPE)
    assert len(records) == 2
    for rec in records:
        assert "request_id" not in rec


def test_fsync_oserror_propagates_no_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill-pair 'fsync-raises -> ok:false, no ack': an fsync OSError on the
    source-of-truth decision journal PROPAGATES out of the primitive, so the
    envelope layer reports ok:false — never a success ack for a non-durable
    write."""

    def _boom_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(io_mod.os, "fsync", _boom_fsync)
    with pytest.raises(OSError, match="simulated fsync failure"):
        _greenlight(tmp_path, request_id="rpc-1")
