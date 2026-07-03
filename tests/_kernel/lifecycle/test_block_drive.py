"""``block-drive`` — the wave-4 stateless resumable tick (block-drive.md §2–§5).

The load-bearing behavior is the §4 routing table + the parked-vs-committed
rendezvous, so most coverage is on the pure planner
:func:`plan_block_action` (no I/O, journal + block-verb faked). A thin set of
:func:`run_tick` integration tests then pins the chaining / park / detached /
terminal control flow with the block-verb subprocess and the journal faked.

Invariant under test throughout (§3): the code NEVER reads a nudge string — it
routes on ``committed_resolved`` (an approved spec) + ownership only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent._kernel.lifecycle import block_drive as bd
from hpc_agent._kernel.lifecycle.block_drive import plan_block_action, run_tick

# ── shared fixtures for the planner ────────────────────────────────────────────


def _pending(
    *,
    workflow: str = "submit",
    current_verb: str = "submit-s2",
    next_verb: str | None = "submit-s3",
    input_spec: dict[str, Any] | None = None,
    cmd_sha: str | None = None,
) -> dict[str, Any]:
    """A pending_decision envelope shaped like ``mark_pending_decision`` writes."""
    return {
        "block": current_verb,
        "workflow": workflow,
        "brief": {"note": "canary green"},
        "resume_cursor": {
            "workflow": workflow,
            "run_id": "r1",
            "next_verb": next_verb,
            "current_verb": current_verb,
            "input_spec": input_spec if input_spec is not None else {"walltime_sec": 100},
        },
        "awaiting_since": "2026-07-03T00:00:00+00:00",
        "cmd_sha": cmd_sha,
    }


# ── plan_block_action: FRESH start ─────────────────────────────────────────────


def test_fresh_start_begins_workflow_first_block() -> None:
    plan = plan_block_action(
        workflow="submit",
        pending_decision={},
        committed_resolved=None,
        last_run_inputs=None,
    )
    assert plan["action"] == "fresh"
    assert plan["verb"] == "submit-s1"  # ORDER["submit"][0]


def test_fresh_start_unknown_workflow_skips() -> None:
    plan = plan_block_action(
        workflow="not-a-workflow",
        pending_decision={},
        committed_resolved=None,
        last_run_inputs=None,
    )
    assert plan["action"] == "skip"


def test_no_pending_no_workflow_skips() -> None:
    plan = plan_block_action(
        workflow=None,
        pending_decision={},
        committed_resolved=None,
        last_run_inputs=None,
    )
    assert plan["action"] == "skip"


# ── plan_block_action: RESUME — awaiting (uncommitted) ─────────────────────────


def test_pending_but_uncommitted_is_awaiting() -> None:
    """A pending decision with no ``response=='y'`` is a valid parked stop (§5)."""
    plan = plan_block_action(
        workflow="submit",
        pending_decision=_pending(),
        committed_resolved=None,  # nothing committed
        last_run_inputs={"walltime_sec": 100},
    )
    assert plan["action"] == "awaiting_decision"
    assert plan["current_verb"] == "submit-s2"


# ── plan_block_action: RESUME — the §4 routing table ───────────────────────────


def test_unchanged_spec_advances() -> None:
    """Plain ``y`` (identical spec) → advance to the code-determined successor."""
    last = {"walltime_sec": 100}
    plan = plan_block_action(
        workflow="submit",
        pending_decision=_pending(input_spec=last),
        committed_resolved=dict(last),  # unchanged
        last_run_inputs=last,
    )
    assert plan["action"] == "advance"
    assert plan["verb"] == "submit-s3"  # the stored next_verb
    assert plan["carry_fields"] == {}


def test_unchanged_via_cmd_sha_fast_path_advances() -> None:
    """Equal cmd_sha ⇒ unchanged even if dict compare is skipped (§4 identity)."""
    plan = plan_block_action(
        workflow="submit",
        pending_decision=_pending(cmd_sha="abc"),
        committed_resolved={"walltime_sec": 999, "cmd_sha": "abc"},
        last_run_inputs={"walltime_sec": 100},
    )
    assert plan["action"] == "advance"


def test_changed_field_owned_by_current_block_reruns() -> None:
    """A nudge editing an S2-owned field at S2 → re-run S2 (recompute derived)."""
    # walltime_sec is owned by submit-s2 (field_ownership.OWNERSHIP["submit"]).
    plan = plan_block_action(
        workflow="submit",
        pending_decision=_pending(current_verb="submit-s2", input_spec={"walltime_sec": 100}),
        committed_resolved={"walltime_sec": 50},  # edited
        last_run_inputs={"walltime_sec": 100},
    )
    assert plan["action"] == "rerun"
    assert plan["verb"] == "submit-s2"
    assert plan["carry_fields"] == {"walltime_sec": 50}


def test_changed_field_owned_downstream_advances_carrying() -> None:
    """The S1 'cap the cost' nudge edits S2's field → advance carrying it (§4)."""
    # At submit-s1, walltime_sec is owned downstream (submit-s2) → advance_carrying.
    plan = plan_block_action(
        workflow="submit",
        pending_decision=_pending(
            current_verb="submit-s1",
            next_verb="submit-s2",
            input_spec={"walltime_sec": 100},
        ),
        committed_resolved={"walltime_sec": 50},
        last_run_inputs={"walltime_sec": 100},
    )
    assert plan["action"] == "advance_carrying"
    assert plan["verb"] == "submit-s2"
    assert plan["carry_fields"] == {"walltime_sec": 50}


def test_unattributed_changed_field_reruns_conservatively() -> None:
    """An unowned changed field → rerun to be safe (field_ownership default)."""
    plan = plan_block_action(
        workflow="submit",
        pending_decision=_pending(current_verb="submit-s2", input_spec={"mystery": 1}),
        committed_resolved={"mystery": 2},
        last_run_inputs={"mystery": 1},
    )
    assert plan["action"] == "rerun"


def test_next_block_metadata_is_not_a_changed_field() -> None:
    """``next_block`` is a routing token, excluded from the diff → still advance."""
    last = {"walltime_sec": 100}
    plan = plan_block_action(
        workflow="submit",
        pending_decision=_pending(input_spec=last),
        committed_resolved={"walltime_sec": 100, "next_block": {"verb": "submit-s3"}},
        last_run_inputs=last,
    )
    assert plan["action"] == "advance"


def test_advance_with_no_successor_is_terminal() -> None:
    """Committed ``y`` at the end of the chain (next_verb None) → terminal."""
    last = {"run_id": "r1"}
    plan = plan_block_action(
        workflow="submit",
        pending_decision=_pending(current_verb="submit-s4", next_verb=None, input_spec=last),
        committed_resolved=dict(last),
        last_run_inputs=last,
    )
    assert plan["action"] == "terminal"


def test_resume_missing_cursor_position_skips() -> None:
    pending = {"workflow": "submit", "resume_cursor": {}}  # no current_verb
    plan = plan_block_action(
        workflow="submit",
        pending_decision=pending,
        committed_resolved={"x": 1},
        last_run_inputs={},
    )
    assert plan["action"] == "skip"


# ── run_tick: chaining / park / detached / terminal (journal + verb faked) ─────


@pytest.fixture
def faked(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the journal reads/writes + the block-verb subprocess.

    Returns a mutable state dict the test drives: ``pending`` (what
    ``read_pending_decision`` returns), ``committed`` (the approved resolved),
    ``results`` (a ``{verb: result_dict}`` map the faked runner serves), and the
    captured ``parked`` / ``cleared`` calls.
    """
    state: dict[str, Any] = {
        "pending": {},
        "committed": None,
        "results": {},
        "parked": [],
        "cleared": [],
        "ran": [],
    }

    monkeypatch.setattr(bd, "read_pending_decision", lambda run_id, **_k: dict(state["pending"]))
    monkeypatch.setattr(bd, "_latest_committed_resolved", lambda *_a, **_k: state["committed"])
    monkeypatch.setattr(
        bd,
        "clear_pending_decision",
        lambda run_id, **_k: state["cleared"].append(run_id),
    )

    def _fake_mark(run_id: str, **kw: Any) -> None:
        state["parked"].append({"run_id": run_id, **kw})

    monkeypatch.setattr(bd, "mark_pending_decision", _fake_mark)

    def _fake_run(verb: str, spec: dict[str, Any], experiment_dir: Path) -> tuple[dict, int]:
        state["ran"].append({"verb": verb, "spec": spec})
        return dict(state["results"].get(verb, {})), 0

    monkeypatch.setattr(bd, "_run_block_verb", _fake_run)
    # Silence the watchdog stamp (it does real journal I/O).
    import hpc_agent._kernel.lifecycle.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_stamp_driver_tick", lambda *_a, **_k: None)
    return state


def test_run_tick_chains_deterministic_spans_in_code(faked: dict[str, Any]) -> None:
    """A clean span with an UNGATED successor chains on IN CODE — no park, no LLM (§2)."""
    faked["results"] = {
        "status-snapshot": {
            "block": "snapshot",
            "stage_reached": "snapshot_clean",
            "needs_decision": False,
            "run_id": "r1",
            "next_block": {"verb": "status-watch", "spec_hint": {"monitor": {"run_id": "r1"}}},
        },
        "status-watch": {
            "block": "watch",
            "stage_reached": "watch_anomaly",
            "needs_decision": True,
            "brief": {"anomaly": "failed"},
            "next_block": None,
        },
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="status")
    assert code == 0
    # snapshot chained into watch (ungated), which then parked on its own decision.
    assert result.action == "awaiting_decision"
    assert [r["verb"] for r in faked["ran"]] == ["status-snapshot", "status-watch"]
    # The chained status-watch ran under its spec_hint VERBATIM (no run_id injection).
    assert faked["ran"][1]["spec"] == {"monitor": {"run_id": "r1"}}
    assert result.current_verb == "status-watch"


def test_run_tick_parks_before_gated_successor(faked: dict[str, Any]) -> None:
    """A clean span whose successor is greenlight-GATED PARKS for the human `y`.

    ``aggregate-check`` reaches ``ready`` with ``needs_decision=False`` and a
    successor of ``aggregate-run`` — but ``aggregate-run`` calls the greenlight
    gate, which an in-code chain never satisfies. So the driver parks BEFORE it
    (needs_decision + gate agree) rather than chaining into a gate failure.
    """
    faked["results"] = {
        "aggregate-check": {
            "block": "check",
            "stage_reached": "ready",
            "needs_decision": False,
            "run_id": "r1",
            "brief": {"terminal": True},
            "next_block": {"verb": "aggregate-run", "spec_hint": {"aggregate": {"run_id": "r1"}}},
        },
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="aggregate")
    assert code == 0
    assert result.action == "awaiting_decision"
    assert result.current_verb == "aggregate-check"
    assert result.next_verb == "aggregate-run"
    # Only the entry block ran; aggregate-run did NOT (it awaits the greenlight).
    assert [r["verb"] for r in faked["ran"]] == ["aggregate-check"]
    assert len(faked["parked"]) == 1
    marker = faked["parked"][0]
    assert marker["resume_cursor"]["next_verb"] == "aggregate-run"


def test_run_tick_parks_at_decision(faked: dict[str, Any]) -> None:
    """A ``needs_decision`` span writes the pending marker and exits (rendezvous)."""
    faked["results"] = {
        "aggregate-check": {
            "block": "check",
            "stage_reached": "not_ready",
            "needs_decision": True,
            "reason": "readiness gate failed",
            "brief": {"missing": ["wave-2"]},
            "next_block": None,
        }
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="aggregate")
    assert code == 0
    assert result.action == "awaiting_decision"
    assert result.current_verb == "aggregate-check"
    assert len(faked["parked"]) == 1
    marker = faked["parked"][0]
    assert marker["block"] == "aggregate-check"
    assert marker["cmd_sha"]  # an identity was stamped
    assert marker["resume_cursor"]["input_spec"] == {"run_id": "r1"}


def test_run_tick_awaiting_when_pending_uncommitted(faked: dict[str, Any]) -> None:
    """Re-entry on a parked-but-uncommitted run does nothing (exit 0, no run)."""
    faked["pending"] = _pending()
    faked["committed"] = None
    result, code = run_tick(Path("."), run_id="r1", workflow="submit")
    assert code == 0
    assert result.action == "awaiting_decision"
    assert faked["ran"] == []
    assert faked["cleared"] == []


def test_run_tick_resume_advance_clears_and_runs_successor(faked: dict[str, Any]) -> None:
    """A committed unchanged spec clears the marker and runs the successor."""
    faked["pending"] = _pending(
        current_verb="submit-s2", next_verb="submit-s3", input_spec={"walltime_sec": 100}
    )
    faked["committed"] = {"walltime_sec": 100}  # unchanged → advance
    faked["results"] = {
        "submit-s3": {
            "block": "s3",
            "stage_reached": "watching_terminal",
            "needs_decision": False,
            "next_block": None,
        }
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="submit")
    assert code == 0
    assert faked["cleared"] == ["r1"]  # consumed the approval
    assert [r["verb"] for r in faked["ran"]] == ["submit-s3"]
    assert result.action == "terminal"


def test_run_tick_detached_exits_with_handle(faked: dict[str, Any]) -> None:
    """A detached, scheduler-bound child → exit with the handle, no chaining (§2).

    Reached via a resume advance into ``submit-s2`` (the real detach path is a
    gated block the human greenlit), which returns a ``started`` handle.
    """
    faked["pending"] = _pending(
        current_verb="submit-s1", next_verb="submit-s2", input_spec={"walltime_sec": 100}
    )
    faked["committed"] = {"walltime_sec": 100}  # unchanged → advance into submit-s2
    faked["results"] = {
        "submit-s2": {
            "block": "s2",
            "stage_reached": "detached",
            "needs_decision": False,
            "started": True,
            "detached_pid": 4242,
            "next_block": None,
        }
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="submit")
    assert code == 0
    assert result.action == "detached"
    assert faked["parked"] == []


def test_run_tick_dry_run_does_not_execute(faked: dict[str, Any]) -> None:
    result, code = run_tick(Path("."), run_id="r1", workflow="aggregate", dry_run=True)
    assert code == 0
    assert faked["ran"] == []


def test_run_tick_block_failure_surfaces_nonzero(faked: dict[str, Any], monkeypatch) -> None:
    """A failed block span (empty result + nonzero exit) is reported as skip."""

    def _fail(verb: str, spec: dict[str, Any], experiment_dir: Path) -> tuple[dict, int]:
        return {}, 7

    monkeypatch.setattr(bd, "_run_block_verb", _fail)
    result, code = run_tick(Path("."), run_id="r1", workflow="aggregate")
    assert code == 7
    assert result.action == "skip"
