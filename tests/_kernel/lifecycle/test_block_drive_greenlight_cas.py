"""S8 — the post-``y`` DOUBLE-DRIVER race on the pending-decision marker.

``docs/plans/run-queue-placement-2026-07-28.md`` §8 S8. Two drivers tick the same
parked run concurrently BY DESIGN (plan §7: the main session's inline first tick
plus the auto-launched drain pass). Before the fix, the consumption leg was a
BLIND ``clear_pending_decision``: both drivers read the same marker pre-clear,
both found the same committed greenlight, both ran the successor span — one
cleared the marker, the other re-parked it, resurrecting a CONSUMED decision
under a stale boundary and double-consuming the human's ``y``.

These tests run the REAL journal (real record files, real ``advisory_flock``
locks, a real decision journal) — only the block-verb subprocess is faked, so the
compare-and-swap is exercised against actual on-disk state:

* two barrier-started threads on one parked run with a committed ``y`` → EXACTLY
  one successor span, exactly one consumption, no resurrection of the consumed
  marker, and the loser's benign "advanced by another driver" disclosure;
* the SEQUENTIAL mismatch: a re-park (new ``awaiting_since``) lands between a
  tick's marker READ and its consume → the tick refuses to spend the STALE
  greenlight against the NEW park;
* the re-park half: a marker whose boundary was consumed and replaced is NOT
  resurrected by the F14 failed-span re-park.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

import hpc_agent._kernel.lifecycle.block_drive as bd
from hpc_agent.state.decision_journal import append_decision, read_decisions
from hpc_agent.state.journal import mark_pending_decision, read_pending_decision, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "cas_run-1"
_WORKFLOW = "aggregate"
_BLOCK = "aggregate-check"
_SUCCESSOR = "aggregate-run"
_PARKED_AT = "2026-07-29T00:30:00+00:00"


def _record(run_id: str = _RUN_ID) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-29T00:00:00+00:00",
        experiment_dir="/exp",
        status="in_flight",
    )


def _park(exp: Path, *, awaiting_since: str = _PARKED_AT) -> None:
    """A REAL parked boundary: an in-flight record plus its pending marker."""
    mark_pending_decision(
        _RUN_ID,
        block=_BLOCK,
        workflow=_WORKFLOW,
        brief={"headline": "aggregate-check reached a decision"},
        resume_cursor={
            "workflow": _WORKFLOW,
            "run_id": _RUN_ID,
            "next_verb": _SUCCESSOR,
            "current_verb": _BLOCK,
            "input_spec": {"run_id": _RUN_ID},
        },
        awaiting_since=awaiting_since,
        cmd_sha="sha-parked",
        experiment_dir=exp,
    )


def _commit_greenlight(exp: Path) -> None:
    """The human's ``y`` for THIS boundary, in the real decision journal."""
    append_decision(
        exp,
        scope_kind="run",
        scope_id=_RUN_ID,
        block=_BLOCK,
        response="y",
        # ``cmd_sha`` echoes the marker's spec identity → the §4 identity
        # fast-path routes this as a plain-``y`` ADVANCE into the successor.
        resolved={
            "aggregate": {"run_id": _RUN_ID},
            "next_block": _SUCCESSOR,
            "cmd_sha": "sha-parked",
        },
    )


@pytest.fixture
def parked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A parked run with a committed ``y``, and a span counter for the successor.

    Only the block-verb SUBPROCESS is faked (spans must not shell out in a unit
    test); every journal read/write below is the real locked implementation. The
    faked span returns a decision result, so the winner parks a NEW boundary —
    which is what makes "the consumed marker was not resurrected" observable.
    """
    upsert_run(tmp_path, _record())
    _park(tmp_path)
    _commit_greenlight(tmp_path)

    state: dict[str, Any] = {"exp": tmp_path, "spans": [], "lock": threading.Lock()}

    def _fake_span(verb: str, spec: dict[str, Any], experiment_dir: Path) -> tuple[dict, int]:
        with state["lock"]:
            state["spans"].append({"verb": verb, "spec": dict(spec)})
        return (
            {
                "block": "run",
                "stage_reached": "harvested",
                "needs_decision": True,
                "brief": {"headline": "aggregate-run wants a decision"},
                "next_block": None,
            },
            0,
        )

    monkeypatch.setattr(bd, "_run_block_verb", _fake_span)
    return state


def test_two_concurrent_drivers_consume_one_greenlight_once(
    parked: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two drivers, one committed ``y``: exactly ONE successor span runs (S8).

    The barrier inside the (real) marker read is what makes the reviewer's race
    deterministic rather than timing-dependent: BOTH threads are held until each
    has read the SAME pre-clear marker — precisely the window in which the blind
    clear let both of them run the span. Everything after the barrier is the real
    code path, including the journal's per-run flock.
    """
    exp = parked["exp"]
    both_have_read = threading.Barrier(2, timeout=30)
    real_read = bd.read_pending_decision

    def _synchronized_read(run_id: str, **kw: Any) -> dict[str, Any]:
        marker = real_read(run_id, **kw)
        both_have_read.wait()
        return marker

    monkeypatch.setattr(bd, "read_pending_decision", _synchronized_read)

    results: dict[int, Any] = {}
    errors: dict[int, BaseException] = {}

    def _tick(slot: int) -> None:
        try:
            results[slot] = bd.run_tick(exp, run_id=_RUN_ID, workflow=_WORKFLOW)
        except BaseException as exc:  # noqa: BLE001 — a raise IS the failure under test
            errors[slot] = exc

    threads = [threading.Thread(target=_tick, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a driver raised instead of losing benignly: {errors}"
    assert len(results) == 2

    # EXACTLY ONE successor span — the whole point.
    assert [span["verb"] for span in parked["spans"]] == [_SUCCESSOR]

    outcomes = {slot: result for slot, (result, _code) in results.items()}
    codes = {code for _result, code in results.values()}
    assert codes == {0}, "neither driver may exit non-zero over a lost race"

    winners = [r for r in outcomes.values() if r.action == "awaiting_decision"]
    losers = [r for r in outcomes.values() if "another driver" in (r.reason or "")]
    assert len(winners) == 1, [r.model_dump() for r in outcomes.values()]
    assert len(losers) == 1, [r.model_dump() for r in outcomes.values()]

    # The LOSER's disclosure: benign, not awaiting, no brief re-surfaced, and
    # classified as PROGRESS so the drain's retryable(n) budget is not charged
    # for a race the design creates (state/journal.DRIVE_PROGRESS_ACTIONS).
    from hpc_agent.state.journal import DRIVE_PROGRESS_ACTIONS

    loser = losers[0]
    assert loser.action == "advanced"
    assert loser.action in DRIVE_PROGRESS_ACTIONS
    assert loser.brief is None
    assert loser.run_id == _RUN_ID

    # The consumed decision was NOT resurrected: the marker on disk is the
    # WINNER's new boundary, not the answered one.
    marker = read_pending_decision(_RUN_ID, experiment_dir=exp)
    assert marker.get("block") == _SUCCESSOR
    assert marker.get("awaiting_since") != _PARKED_AT

    # Exactly one consumption: nothing re-journaled the human's y, and the
    # single greenlight is now behind the newer park.
    records = read_decisions(exp, "run", _RUN_ID)
    assert [rec["response"] for rec in records] == ["y"]


def test_stale_marker_read_refuses_to_consume_against_a_new_park(
    parked: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEQUENTIAL CAS mismatch: a re-park between the read and the consume.

    The tick reads marker ``(aggregate-check, T0)``; before it consumes, the
    boundary is re-parked with a NEW ``awaiting_since`` (T1) — the same shape a
    concurrent driver's failed-span re-park or a re-run of the block produces.
    The tick must NOT spend the stale greenlight against the new park: no span,
    no clear, the newer marker intact.
    """
    exp = parked["exp"]
    stale = read_pending_decision(_RUN_ID, experiment_dir=exp)
    assert stale.get("awaiting_since") == _PARKED_AT

    # The re-park lands between the read and the consume.
    _park(exp, awaiting_since="2026-07-29T01:15:00+00:00")

    monkeypatch.setattr(bd, "read_pending_decision", lambda *_a, **_k: dict(stale))

    result, code = bd.run_tick(exp, run_id=_RUN_ID, workflow=_WORKFLOW)

    assert code == 0
    assert parked["spans"] == [], "the stale greenlight must not run a span"
    assert result.action == "advanced"
    assert "another driver" in (result.reason or "")

    # The NEW park survived untouched — the stale tick cleared nothing.
    marker = read_pending_decision(_RUN_ID, experiment_dir=exp)
    assert marker.get("block") == _BLOCK
    assert marker.get("awaiting_since") == "2026-07-29T01:15:00+00:00"


def test_failed_span_repark_does_not_resurrect_a_replaced_boundary(
    parked: dict[str, Any],
) -> None:
    """The re-park half of S8: a re-park only lands in an EMPTY marker slot.

    ``_repark_marker`` is the F14 crash-consistency leg (a resume span failed, so
    the approval was NOT consumed and the marker must come back). Blind, it can
    resurrect a boundary another driver has already consumed and moved past. The
    compare-and-swap expects the slot to still be empty, so the newer park wins.
    """
    from hpc_agent.state.journal import (
        compare_and_clear_pending_decision,
        compare_and_repark_pending_decision,
    )

    exp = parked["exp"]
    stale = read_pending_decision(_RUN_ID, experiment_dir=exp)

    # The winner consumes the boundary and parks a NEW one.
    assert compare_and_clear_pending_decision(
        _RUN_ID, block=_BLOCK, awaiting_since=_PARKED_AT, experiment_dir=exp
    )
    # A second consume of the same boundary loses — one greenlight, one consumer.
    assert not compare_and_clear_pending_decision(
        _RUN_ID, block=_BLOCK, awaiting_since=_PARKED_AT, experiment_dir=exp
    )
    mark_pending_decision(
        _RUN_ID,
        block=_SUCCESSOR,
        workflow=_WORKFLOW,
        brief={},
        resume_cursor={"workflow": _WORKFLOW, "run_id": _RUN_ID, "current_verb": _SUCCESSOR},
        awaiting_since="2026-07-29T02:00:00+00:00",
        experiment_dir=exp,
    )

    # The loser's failed span now tries to re-park the CONSUMED boundary.
    bd._repark_marker(exp, _RUN_ID, stale)

    marker = read_pending_decision(_RUN_ID, experiment_dir=exp)
    assert marker.get("block") == _SUCCESSOR
    assert marker.get("awaiting_since") == "2026-07-29T02:00:00+00:00"

    # …and into an EMPTY slot the same re-park DOES land (F14 still holds).
    assert compare_and_clear_pending_decision(
        _RUN_ID,
        block=_SUCCESSOR,
        awaiting_since="2026-07-29T02:00:00+00:00",
        experiment_dir=exp,
    )
    assert compare_and_repark_pending_decision(
        _RUN_ID,
        block=str(stale.get("block")),
        workflow=str(stale.get("workflow")),
        brief=dict(stale.get("brief") or {}),
        resume_cursor=dict(stale.get("resume_cursor") or {}),
        awaiting_since=str(stale.get("awaiting_since")),
        experiment_dir=exp,
    )
    restored = read_pending_decision(_RUN_ID, experiment_dir=exp)
    assert restored.get("block") == _BLOCK
    assert restored.get("resume_cursor") == stale.get("resume_cursor")
