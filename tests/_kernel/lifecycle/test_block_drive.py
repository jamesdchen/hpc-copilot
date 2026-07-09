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


# ── plan_block_action: journal-derived resume (run #9, no marker parked) ───────


def test_no_pending_committed_greenlight_resumes_from_journal() -> None:
    """An interview-driven chain never parks a marker (submit-s1 runs by direct
    invocation), but the committed greenlight's next_block IS a durable cursor —
    the tick advances there instead of skipping (run #9: the skip forced the
    agent to hand-dispatch submit-s2)."""
    plan = plan_block_action(
        workflow=None,
        pending_decision={},
        committed_resolved={"next_block": "submit-s2", "submit": {"submit": {"run_id": "r1"}}},
        last_run_inputs=None,
    )
    assert plan["action"] == "advance"
    assert plan["verb"] == "submit-s2"
    assert plan["workflow"] == "submit"


def test_journal_resume_scoped_to_the_verbs_own_workflow() -> None:
    """An explicit MISMATCHING workflow request wins over the journal cursor —
    a status tick against a run mid-submit fresh-starts status, never re-routes
    into the submit chain."""
    plan = plan_block_action(
        workflow="status",
        pending_decision={},
        committed_resolved={"next_block": "submit-s2"},
        last_run_inputs=None,
    )
    assert plan["action"] == "fresh"
    assert plan["verb"] == "status-snapshot"


def test_committed_without_next_block_fresh_starts() -> None:
    """A committed decision naming no (or an unknown) next_block verb is not a
    cursor — the tick falls through to the plain fresh start."""
    plan = plan_block_action(
        workflow="submit",
        pending_decision={},
        committed_resolved={"goal": "estimate pi"},
        last_run_inputs=None,
    )
    assert plan["action"] == "fresh"
    assert plan["verb"] == "submit-s1"

    plan = plan_block_action(
        workflow="submit",
        pending_decision={},
        committed_resolved={"next_block": "not-a-verb"},
        last_run_inputs=None,
    )
    assert plan["action"] == "fresh"
    assert plan["verb"] == "submit-s1"


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


def _gated_park_results() -> dict[str, Any]:
    """A clean entry block whose gated successor forces a park (the rendezvous).

    ``aggregate-check`` is bare-startable from a ``(run_id, workflow)`` tick, so
    this drives ``run_tick`` all the way into ``_park`` — unlike ``submit-s1``,
    which is not bare-startable and short-circuits to ``skip`` before parking.
    The ``_park`` code path (and its ``mark_pending_decision`` call) is identical
    for both boundaries, so this faithfully exercises the sidecar-only crash.
    """
    return {
        "aggregate-check": {
            "block": "check",
            "stage_reached": "ready",
            "needs_decision": False,
            "run_id": "r1",
            "brief": {"terminal": True},
            "next_block": {"verb": "aggregate-run", "spec_hint": {"aggregate": {"run_id": "r1"}}},
        },
    }


def test_run_tick_parks_sidecar_only_run_without_a_record_does_not_crash(
    faked: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar-only run (no journal RunRecord) PARKS without crashing the tick.

    notebook-audit.md Addendum 13.0: the journal RunRecord is minted by
    ``submit_and_record`` INSIDE the gated submit-s2 (qsub) — S1's resolve leg
    writes only the per-run sidecar. So the FIRST park (the S1→S2 greenlight
    gate) is reached before any record exists, and ``mark_pending_decision`` →
    ``update_run_status`` raises FileNotFoundError for the record-less run. That
    crashed the driver tick at the rendezvous for both of run #11's runs. The
    park must DEGRADE DISCLOSED: the human still gets the brief
    (``awaiting_decision``), only the durable journal marker is skipped. FIRES on
    the unguarded call (FileNotFoundError propagates out of the tick).
    """

    def _raise_missing(run_id: str, **_kw: Any) -> None:
        raise FileNotFoundError(f"no run record for {run_id!r}")

    monkeypatch.setattr(bd, "mark_pending_decision", _raise_missing)
    faked["results"] = _gated_park_results()
    # Must NOT raise even though the marker write hits a missing record.
    result, code = run_tick(Path("."), run_id="r1", workflow="aggregate")
    assert code == 0
    assert result.action == "awaiting_decision"
    assert result.current_verb == "aggregate-check"
    assert result.next_verb == "aggregate-run"
    # The brief still rode out to the human (disclosure survived) …
    assert result.brief == {"terminal": True}
    # … but no durable marker was persisted (the raise swallowed, not appended).
    assert faked["parked"] == []


def test_run_tick_parks_with_a_record_still_writes_the_marker(
    faked: dict[str, Any],
) -> None:
    """The normal park (a journal RunRecord exists) still persists the §5 marker.

    The record-less guard added for Addendum 13.0 must not weaken the happy path:
    when ``mark_pending_decision`` succeeds, the durable pending-decision marker
    (the "parked ≠ stalled" flag + resume_cursor) is still written.
    """
    faked["results"] = _gated_park_results()
    result, code = run_tick(Path("."), run_id="r1", workflow="aggregate")
    assert code == 0
    assert result.action == "awaiting_decision"
    assert result.next_verb == "aggregate-run"
    assert len(faked["parked"]) == 1
    marker = faked["parked"][0]
    assert marker["run_id"] == "r1"
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


def test_run_tick_resumes_from_journal_without_marker(faked: dict[str, Any]) -> None:
    """Run #9: an interview-driven chain has NO parked marker, only the committed
    greenlight. The tick must run the named block under the committed resolved
    (routing token stripped) and park at its rendezvous — re-onboarding the
    hand-started chain onto the driver — instead of reporting nothing drivable."""
    faked["pending"] = {}
    faked["committed"] = {"next_block": "submit-s2", "submit": {"submit": {"run_id": "r1"}}}
    faked["results"] = {
        "submit-s2": {
            "block": "s2",
            "stage_reached": "canary_verified",
            "needs_decision": True,
            "run_id": "r1",
            "brief": {"verified": True},
            "next_block": {"verb": "submit-s3", "why": "launch", "spec_hint": {}},
        },
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="submit")
    assert code == 0
    assert [r["verb"] for r in faked["ran"]] == ["submit-s2"]
    # The block ran under the committed resolved minus the routing token.
    assert faked["ran"][0]["spec"] == {"submit": {"submit": {"run_id": "r1"}}}
    # Parked at the S2 brief — the marker now exists, so later ticks resume normally.
    assert result.action == "awaiting_decision"
    assert len(faked["parked"]) == 1


def test_run_tick_block_failure_surfaces_nonzero(faked: dict[str, Any], monkeypatch) -> None:
    """A failed block span (empty result + nonzero exit) is reported as skip."""

    def _fail(verb: str, spec: dict[str, Any], experiment_dir: Path) -> tuple[dict, int]:
        return {}, 7

    monkeypatch.setattr(bd, "_run_block_verb", _fail)
    result, code = run_tick(Path("."), run_id="r1", workflow="aggregate")
    assert code == 7
    assert result.action == "skip"


# ── run_tick: the acting spec is filtered against the TARGET block's model ─────


def test_spec_model_field_names_resolve_from_the_registry() -> None:
    """The filter reads each block's DECLARED spec fields, so a genuine input
    like aggregate-check's required run_id survives while aggregate-run (whose
    model declares only ``aggregate``) sheds the same key as an echo."""
    check_fields = bd._spec_model_field_names("aggregate-check")
    assert check_fields is not None and "run_id" in check_fields
    run_fields = bd._spec_model_field_names("aggregate-run")
    assert run_fields is not None and "run_id" not in run_fields
    assert bd._spec_model_field_names("not-a-verb") is None


def test_run_tick_resume_strips_journal_identity_echoes(faked: dict[str, Any]) -> None:
    """The committed ``resolved`` legitimately carries the journal-sanctioned
    identity echoes (cmd_sha / run_id / total_tasks — ops/decision/journal.py),
    but every block spec model is extra='forbid': the acting spec must keep
    ONLY the target model's declared fields, exactly on the §4 identity
    fast-path inputs (equal cmd_sha → advance)."""
    faked["pending"] = _pending(
        workflow="aggregate",
        current_verb="aggregate-check",
        next_verb="aggregate-run",
        input_spec={"run_id": "r1"},
        cmd_sha="abc",
    )
    faked["committed"] = {
        "aggregate": {"run_id": "r1"},
        "run_id": "r1",
        "cmd_sha": "abc",
        "total_tasks": 4,
        "next_block": "aggregate-run",
    }
    faked["results"] = {
        "aggregate-run": {
            "block": "run",
            "stage_reached": "harvested",
            "needs_decision": True,
            "brief": {},
            "next_block": None,
        }
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="aggregate")
    assert code == 0
    assert result.action == "awaiting_decision"
    # The echoes are gone; the declared input survived.
    assert faked["ran"][0]["spec"] == {"aggregate": {"run_id": "r1"}}


def test_run_tick_resume_validates_against_the_real_spec_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the REAL ``_run_block_verb`` subprocess seam: the
    child validates the acting spec with the real ``extra='forbid'``
    AggregateRunSpec, which the fully-faked runner tests never crossed — the
    escape hatch this bug lived in. With the echoes stripped the resumed span
    validates; before the fix it failed ``extra_forbidden`` on every tick."""
    import sys

    pending = _pending(
        workflow="aggregate",
        current_verb="aggregate-check",
        next_verb="aggregate-run",
        input_spec={"run_id": "r1"},
        cmd_sha="abc",
    )
    committed = {
        "aggregate": {"run_id": "r1"},
        "run_id": "r1",
        "cmd_sha": "abc",
        "next_block": "aggregate-run",
    }
    monkeypatch.setattr(bd, "read_pending_decision", lambda run_id, **_k: dict(pending))
    monkeypatch.setattr(bd, "_latest_committed_resolved", lambda *_a, **_k: dict(committed))
    monkeypatch.setattr(bd, "clear_pending_decision", lambda *_a, **_k: None)
    monkeypatch.setattr(bd, "mark_pending_decision", lambda *_a, **_k: None)
    import hpc_agent._kernel.lifecycle.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_stamp_driver_tick", lambda *_a, **_k: None)

    child = (
        "import json, sys\n"
        "from hpc_agent._wire.workflows.aggregate_blocks import AggregateRunSpec\n"
        "with open(sys.argv[1], encoding='utf-8') as fh:\n"
        "    AggregateRunSpec.model_validate(json.load(fh))\n"
        "print(json.dumps({'data': {'block': 'run', 'stage_reached': 'harvested',"
        " 'needs_decision': True, 'brief': {}, 'next_block': None}}))\n"
    )
    monkeypatch.setattr(
        bd,
        "_block_verb_argv",
        lambda verb, spec_path, experiment_dir: [sys.executable, "-c", child, spec_path],
    )
    result, code = run_tick(tmp_path, run_id="r1", workflow="aggregate")
    assert code == 0, result.reason
    assert result.action == "awaiting_decision"


# ── run_tick: a failed resume span re-parks the marker (crash consistency) ─────


def test_run_tick_failed_rerun_reparks_marker_and_next_tick_retries_rerun(
    faked: dict[str, Any],
) -> None:
    """A rerun whose span FAILS must not destroy the resume cursor: the marker
    is re-parked verbatim, so the next tick routes the SAME rerun (the human's
    nudge) instead of degrading to a journal-derived advance."""
    faked["pending"] = _pending(current_verb="submit-s2", input_spec={"walltime_sec": 100})
    faked["committed"] = {"walltime_sec": 50}  # S2-owned edit → rerun
    faked["results"] = {}  # submit-s2 span fails (empty result)

    result, code = run_tick(Path("."), run_id="r1", workflow="submit")
    assert code != 0
    assert result.action == "skip"
    assert faked["cleared"] == ["r1"]
    # The marker was re-parked verbatim (cursor + input_spec diff base intact).
    assert len(faked["parked"]) == 1
    reparked = faked["parked"][0]
    assert reparked["block"] == "submit-s2"
    assert reparked["resume_cursor"]["input_spec"] == {"walltime_sec": 100}

    # Next tick: the marker is back, the same commit routes to RERUN again.
    faked["pending"] = {k: v for k, v in reparked.items() if k != "run_id"}
    faked["results"] = {
        "submit-s2": {
            "block": "s2",
            "stage_reached": "canary_verified",
            "needs_decision": True,
            "brief": {},
            "next_block": {"verb": "submit-s3", "why": "launch", "spec_hint": {}},
        }
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="submit")
    assert code == 0
    assert [r["verb"] for r in faked["ran"]] == ["submit-s2", "submit-s2"]  # rerun, not advance


def test_run_tick_later_chained_span_failure_does_not_repark(faked: dict[str, Any]) -> None:
    """Once the FIRST resumed span succeeded the approval WAS consumed — a
    failure in a later chained span must not resurrect the marker (that would
    double-consume the decision on the next tick)."""
    faked["pending"] = _pending(
        current_verb="submit-s2", next_verb="submit-s3", input_spec={"walltime_sec": 100}
    )
    faked["committed"] = {"walltime_sec": 100}  # unchanged → advance to submit-s3
    faked["results"] = {
        "submit-s3": {
            "block": "s3",
            "stage_reached": "watching_timeout",
            "needs_decision": False,
            "next_block": {"verb": "status-watch", "why": "keep watching", "spec_hint": {}},
        }
        # status-watch missing → the CHAINED span fails.
    }
    result, code = run_tick(Path("."), run_id="r1", workflow="submit")
    assert code != 0
    assert result.action == "skip"
    assert faked["cleared"] == ["r1"]
    assert faked["parked"] == []


# ── run_tick: fresh-entry specs the driver cannot build → clear skip ───────────


def test_run_tick_bare_aggregate_without_run_id_skips_naming_the_input(
    faked: dict[str, Any],
) -> None:
    """aggregate-check's spec REQUIRES run_id: a bare ``--workflow aggregate``
    tick gets the documented clear skip naming the missing input, never a
    doomed SpecInvalid span."""
    result, code = run_tick(Path("."), run_id=None, workflow="aggregate")
    assert code == 0
    assert result.action == "skip"
    assert "run_id" in result.reason
    assert faked["ran"] == []


def test_run_tick_bare_status_without_run_id_still_runs_fleet_digest(
    faked: dict[str, Any],
) -> None:
    """status-snapshot's run_id is optional — the ``{}`` fleet digest stays."""
    faked["results"] = {
        "status-snapshot": {
            "block": "snapshot",
            "stage_reached": "snapshot_clean",
            "needs_decision": False,
            "next_block": None,
        }
    }
    result, code = run_tick(Path("."), run_id=None, workflow="status")
    assert code == 0
    assert faked["ran"] == [{"verb": "status-snapshot", "spec": {}}]
    assert result.action == "terminal"
