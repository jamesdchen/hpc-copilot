"""Tests for the ``block-drive`` decision-rendezvous hook pair (§5).

The pair generalizes the skill-return Stop guard + PostToolUse autofetch to the
``block-drive`` y/nudge boundary (``docs/design/block-drive.md`` §5):

* ``decision_rendezvous_stop_guard`` — a ``Stop`` hook that forces the driver to
  advance ONLY when a human ``y`` is committed to the decision journal but the
  ``pending_decision`` marker is still set (Phase-4 stall). It stays **silent**
  while the driver is merely awaiting the human (Phase 2/3a) — the whole §5
  subtlety: never force continuation into a void.
* ``decision_rendezvous_autofetch`` — a ``PostToolUse`` hook that injects the
  brief a ``block-drive`` tick just parked so the LLM reliably renders it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import decision_rendezvous_autofetch as fetch
from hpc_agent._kernel.hooks import decision_rendezvous_stop_guard as guard
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import mark_pending_decision, upsert_run
from hpc_agent.state.run_record import RunRecord

_RUN_ID = "run-abc"
_BLOCK = "s2"
_WORKFLOW = "submit"
_BRIEF = {"proposal": "canary looks good", "cost": 42}


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Isolate the journal home (also isolates the skill-return breadcrumb the
    # Stop guard scans, which lives under _current_homedir()).
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str = _RUN_ID, *, status: str = "in_flight") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status=status,
    )


def _park(exp: Path, run_id: str = _RUN_ID) -> None:
    """Upsert an in-flight run and stamp its pending_decision marker + brief."""
    upsert_run(exp, _record(run_id))
    mark_pending_decision(
        run_id,
        block=_BLOCK,
        workflow=_WORKFLOW,
        brief=_BRIEF,
        resume_cursor={
            "workflow": _WORKFLOW,
            "run_id": run_id,
            "next_verb": "s3",
            "current_verb": "s2",
        },
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=exp,
    )


def _commit_y(exp: Path, run_id: str = _RUN_ID) -> None:
    # A real greenlight names the parked boundary's successor in ``resolved``
    # (block_gate ``next_block``); the marker parked by ``_park`` has next_verb
    # "s3", and the auto-stamped ``ts`` (now) is after ``awaiting_since``.
    append_decision(
        exp,
        scope_kind="run",
        scope_id=run_id,
        block=_BLOCK,
        response="y",
        resolved={"approved": True, "next_block": "s3"},
    )


def _commit_nudge(exp: Path, run_id: str = _RUN_ID) -> None:
    append_decision(
        exp,
        scope_kind="run",
        scope_id=run_id,
        block=_BLOCK,
        response="cap the cost at 10",
    )


def _stop_payload(exp: Path, *, stop_hook_active: bool = False) -> dict:
    return {
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
        "cwd": str(exp),
    }


# ─── Stop guard: force continue only when marker + `y` both present ─────────


def test_marker_plus_committed_y_forces_continue(tmp_path: Path) -> None:
    _park(tmp_path)
    _commit_y(tmp_path)

    out = guard.build_hook_output(_stop_payload(tmp_path))

    assert out is not None
    assert out["decision"] == "block"
    assert _RUN_ID in out["reason"]
    assert _BLOCK in out["reason"]
    assert "block-drive" in out["reason"]
    assert _WORKFLOW in out["reason"]


# ─── the §5 subtlety: silent while genuinely awaiting the human ─────────────


def test_parked_without_any_decision_is_silent(tmp_path: Path) -> None:
    """Phase 2: parked, nothing committed → waiting for the human is a valid
    stop. The guard must NOT force continuation into a void."""
    _park(tmp_path)
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None


def test_parked_with_trailing_nudge_is_silent(tmp_path: Path) -> None:
    """Phase 3a: the human nudged (latest decision is not a `y`) → still
    awaiting a fresh `y`. Silent."""
    _park(tmp_path)
    _commit_nudge(tmp_path)
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None


def test_y_then_nudge_latest_wins_is_silent(tmp_path: Path) -> None:
    """A `y` followed by a later nudge → latest is the nudge → silent (the
    guard keys on the LATEST decision, not any historical `y`)."""
    _park(tmp_path)
    _commit_y(tmp_path)
    _commit_nudge(tmp_path)
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None


def test_committed_y_but_no_marker_is_silent(tmp_path: Path) -> None:
    """A `y` on the journal but no pending_decision marker means the driver
    already advanced (marker cleared) → nothing to force."""
    upsert_run(tmp_path, _record())
    _commit_y(tmp_path)
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None


def _commit_unrelated_later(exp: Path, run_id: str = _RUN_ID) -> None:
    """An UNRELATED later touchpoint under a DIFFERENT block (e.g. an overnight-consent)."""
    append_decision(
        exp,
        scope_kind="run",
        scope_id=run_id,
        block="overnight-consent",
        response="let it run overnight to the canary, cap 50 dollars",
    )


def test_y_then_unrelated_later_record_still_forces_continue(tmp_path: Path) -> None:
    """F13 direction (b): a committed `y` followed by an UNRELATED later record (a
    different block's touchpoint — an overnight-consent, a sign-off) must NOT silence the
    guard. Previously the guard tested only ``decisions[-1]``, so the trailing consent
    hid the genuine committed-but-unadvanced `y` and the 2026-06-10 stall re-opened. The
    shared newest-first scan skips the unrelated block and still finds the `y`."""
    _park(tmp_path)
    _commit_y(tmp_path)
    _commit_unrelated_later(tmp_path)

    out = guard.build_hook_output(_stop_payload(tmp_path))

    assert out is not None
    assert out["decision"] == "block"
    assert _RUN_ID in out["reason"]


# ─── boundary scoping: a consumed greenlight is not THIS boundary's ──────────
# (bug-sweep 2026-07-11 #23 / run-12 finding 21)


def test_prior_boundary_consumed_y_is_silent(tmp_path: Path) -> None:
    """bug-sweep #23: parked at s2→s3, but the journal's latest `y` names the
    PREVIOUS boundary (s2 — already consumed, nothing appended on consumption).
    The run is still awaiting the human, so the guard stays silent instead of
    force-continuing into a void on every turn end."""
    _park(tmp_path)  # marker next_verb == "s3"
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block=_BLOCK,
        response="y",
        resolved={"approved": True, "next_block": "s2"},
    )
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None
    assert guard.find_committed_unadvanced(tmp_path) is None


def test_reparked_stale_same_boundary_y_is_silent(tmp_path: Path) -> None:
    """run-12 finding 21: the `y` DID name this boundary (s3) but predates the
    (re-)park's awaiting_since (a tick consumed it, ran the block, re-parked) →
    a consumed greenlight, so the guard stays silent."""
    _park(tmp_path)  # awaiting_since == 2026-07-03T00:30:00+00:00
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block=_BLOCK,
        response="y",
        resolved={"approved": True, "next_block": "s3"},
        ts="2026-07-03T00:00:00+00:00",  # BEFORE the park → already consumed
    )
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None


def test_fresh_boundary_targeting_y_forces_continue(tmp_path: Path) -> None:
    """The 2026-06-10 stall class stays closed: a fresh `y` naming this boundary
    (s3), journaled after the park, still forces continuation."""
    _park(tmp_path)
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block=_BLOCK,
        response="y",
        resolved={"approved": True, "next_block": "s3"},
        ts="2026-07-03T01:00:00+00:00",  # AFTER the park → live greenlight
    )
    out = guard.build_hook_output(_stop_payload(tmp_path))
    assert out is not None
    assert out["decision"] == "block"


# ─── campaign scope: greenlights are journaled under scope "campaign" ────────

_CAMPAIGN_ID = "camp-1"


def _park_campaign(exp: Path, campaign_id: str = _CAMPAIGN_ID) -> None:
    """Park a campaign chain: marker workflow 'campaign', keyed by campaign id
    (block_drive drives a campaign chain under --run-id <campaign_id>)."""
    upsert_run(exp, _record(campaign_id))
    mark_pending_decision(
        campaign_id,
        block="campaign-greenlight",
        workflow="campaign",
        brief=_BRIEF,
        resume_cursor={
            "workflow": "campaign",
            "run_id": campaign_id,
            "next_verb": "campaign-watch",
            "current_verb": "campaign-greenlight",
        },
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=exp,
    )


def test_campaign_scoped_committed_y_forces_continue(tmp_path: Path) -> None:
    """A campaign-workflow greenlight is journaled under scope 'campaign'
    (mirroring block_drive.run_tick's read) — the guard must find it there or
    the committed-but-unadvanced block can never fire for campaign chains."""
    _park_campaign(tmp_path)
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        block="campaign-greenlight",
        response="y",
        resolved={"approved": True, "next_block": "campaign-watch"},
    )

    out = guard.build_hook_output(_stop_payload(tmp_path))

    assert out is not None
    assert out["decision"] == "block"
    assert _CAMPAIGN_ID in out["reason"]
    assert "campaign" in out["reason"]


def test_campaign_parked_with_trailing_nudge_is_silent(tmp_path: Path) -> None:
    """The §5 subtlety holds in the campaign scope too: a trailing nudge means
    still awaiting the human — the guard stays silent."""
    _park_campaign(tmp_path)
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        block="campaign-greenlight",
        response="raise the budget to 500 core-hours",
    )
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None


def test_campaign_marker_ignores_run_scope_decisions(tmp_path: Path) -> None:
    """A campaign-parked chain keys on the CAMPAIGN journal — a `y` sitting in
    the (empty-marker) run scope of the same id must not arm the guard."""
    _park_campaign(tmp_path)
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_CAMPAIGN_ID,
        block="campaign-greenlight",
        response="y",
        resolved={"approved": True},
    )
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None


# ─── loop safety & defensive no-ops ─────────────────────────────────────────


def test_stop_hook_active_is_noop_even_when_armed(tmp_path: Path) -> None:
    _park(tmp_path)
    _commit_y(tmp_path)
    assert guard.build_hook_output(_stop_payload(tmp_path, stop_hook_active=True)) is None


def test_no_runs_is_noop(tmp_path: Path) -> None:
    assert guard.build_hook_output(_stop_payload(tmp_path)) is None


def test_malformed_payload_is_noop() -> None:
    bad_payloads: list[object] = [None, [], "string", 42]
    for bad in bad_payloads:
        assert guard.build_hook_output(bad) is None


# ─── find_committed_unadvanced unit ─────────────────────────────────────────


def test_find_committed_unadvanced_returns_block_and_workflow(tmp_path: Path) -> None:
    _park(tmp_path)
    _commit_y(tmp_path)
    hit = guard.find_committed_unadvanced(tmp_path)
    assert hit == {"run_id": _RUN_ID, "block": _BLOCK, "workflow": _WORKFLOW}


def test_find_committed_unadvanced_none_when_only_nudge(tmp_path: Path) -> None:
    _park(tmp_path)
    _commit_nudge(tmp_path)
    assert guard.find_committed_unadvanced(tmp_path) is None


# ─── Stop guard main() stdin/stdout wrapper ─────────────────────────────────


def _run_stop_main(monkeypatch, stdin_text: str) -> tuple[int, str]:
    out_buf = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    monkeypatch.setattr("sys.stdout", out_buf)
    rc = guard.main([])
    return rc, out_buf.getvalue()


def test_stop_main_armed_prints_block(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path)
    _commit_y(tmp_path)
    rc, out = _run_stop_main(monkeypatch, json.dumps(_stop_payload(tmp_path)))
    assert rc == 0
    assert json.loads(out)["decision"] == "block"


def test_stop_main_waiting_prints_nothing(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path)
    rc, out = _run_stop_main(monkeypatch, json.dumps(_stop_payload(tmp_path)))
    assert rc == 0
    assert out == ""


def test_stop_main_never_raises_on_core_error(tmp_path: Path, monkeypatch) -> None:
    def _boom(_payload):
        raise RuntimeError("simulated core failure")

    monkeypatch.setattr(guard, "build_hook_output", _boom)
    rc, out = _run_stop_main(monkeypatch, json.dumps(_stop_payload(tmp_path)))
    assert rc == 0
    assert out == ""


# ─── PostToolUse autofetch: inject the parked brief ─────────────────────────


def _bash_payload(command: str, *, cwd: str | None = None) -> dict:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def test_autofetch_injects_brief_after_block_drive(tmp_path: Path) -> None:
    _park(tmp_path)
    cmd = f"hpc-agent block-drive --run-id {_RUN_ID} --experiment-dir {tmp_path}"
    out = fetch.build_hook_output(_bash_payload(cmd, cwd=str(tmp_path)))

    assert out is not None
    hs = out["hookSpecificOutput"]
    assert hs["hookEventName"] == "PostToolUse"
    assert json.loads(hs["additionalContext"]) == _BRIEF


def test_autofetch_experiment_dir_from_cwd_fallback(tmp_path: Path) -> None:
    _park(tmp_path)
    cmd = f"hpc-agent block-drive --run-id {_RUN_ID}"
    out = fetch.build_hook_output(_bash_payload(cmd, cwd=str(tmp_path)))
    assert out is not None
    assert json.loads(out["hookSpecificOutput"]["additionalContext"]) == _BRIEF


def test_autofetch_noop_when_not_parked(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record())  # in-flight but no marker
    cmd = f"hpc-agent block-drive --run-id {_RUN_ID} --experiment-dir {tmp_path}"
    assert fetch.build_hook_output(_bash_payload(cmd, cwd=str(tmp_path))) is None


def test_autofetch_noop_on_non_bash(tmp_path: Path) -> None:
    _park(tmp_path)
    assert fetch.build_hook_output({"tool_name": "Read", "tool_input": {}}) is None


def test_autofetch_noop_on_unrelated_bash(tmp_path: Path) -> None:
    _park(tmp_path)
    assert fetch.build_hook_output(_bash_payload("ls -la", cwd=str(tmp_path))) is None


def test_autofetch_malformed_payload_is_noop() -> None:
    bad_payloads: list[object] = [None, [], "string", 42]
    for bad in bad_payloads:
        assert fetch.build_hook_output(bad) is None


def test_extract_drive_invocation_parses_run_id_and_dir() -> None:
    cmd = "hpc-agent block-drive --run-id r-1 --experiment-dir '/tmp/exp'"
    assert fetch.extract_drive_invocation(cmd) == ("r-1", "/tmp/exp")


def test_extract_drive_invocation_none_without_block_drive() -> None:
    assert fetch.extract_drive_invocation("hpc-agent doctor --run-id r-1") is None


# ─── the COMPLETER (RULED 2026-07-12): run the mechanical tick in code ────────
#
# Dark by default: with no capability declared, every Stop-guard test ABOVE
# exercises the REJECTOR verbatim. Activating HPC_STOP_HOOK_APPEND flips the
# completer on — code runs the parked block-drive tick itself, gated on a
# MECHANICAL next verb + a HEALTHY transport (breaker closed). The fork-exhaustion
# night (finding 20/21) is the breaker-open counter-example: the completer must
# refuse and degrade to the rejector byte-for-byte.

from hpc_agent.infra import ssh_circuit  # noqa: E402
from hpc_agent.state.journal import clear_pending_decision  # noqa: E402

_MECH_NEXT_VERB = "submit-s3"


def _park_mechanical(exp: Path, run_id: str = _RUN_ID) -> None:
    """Park at a boundary whose successor is a REAL chain block (mechanical)."""
    upsert_run(exp, _record(run_id))
    mark_pending_decision(
        run_id,
        block="submit-s2",
        workflow=_WORKFLOW,
        brief=_BRIEF,
        resume_cursor={
            "workflow": _WORKFLOW,
            "run_id": run_id,
            "next_verb": _MECH_NEXT_VERB,
            "current_verb": "submit-s2",
        },
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=exp,
    )


def _commit_y_targeting(exp: Path, next_block: str, run_id: str = _RUN_ID) -> None:
    append_decision(
        exp,
        scope_kind="run",
        scope_id=run_id,
        block="submit-s2",
        response="y",
        resolved={"approved": True, "next_block": next_block},
    )


def _activate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_STOP_HOOK_APPEND", "1")


def _open_breaker(host: str = "h") -> None:
    """Trip the SSH circuit for *host* — a genuinely-cooling OPEN state."""
    import time

    path = ssh_circuit.circuit_state_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = ssh_circuit._fresh_doc(host)
    doc.update({"state": "open", "opened_at": time.time(), "cooldown_sec": 300.0})
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_completer_dark_default_is_rejector_identical(tmp_path: Path, monkeypatch) -> None:
    """With NO capability declared (the default landing) the mechanical advance
    the completer would run instead BOUNCES — rejector-identical."""
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND", raising=False)
    _park_mechanical(tmp_path)
    _commit_y_targeting(tmp_path, _MECH_NEXT_VERB)

    out = guard.build_hook_output(_stop_payload(tmp_path))
    assert out is not None
    assert out["decision"] == "block"
    assert "systemMessage" not in out
    assert "block-drive" in out["reason"]


def test_completer_advance_clears_marker_no_bounce(tmp_path: Path, monkeypatch) -> None:
    """Capability declared + mechanical verb + healthy transport → code runs the
    tick; on advance the marker clears and the stop PROCEEDS (no bounce)."""
    _activate(monkeypatch)
    _park_mechanical(tmp_path)
    _commit_y_targeting(tmp_path, _MECH_NEXT_VERB)

    called: dict = {}

    def _fake_tick(exp: Path, run_id: str, workflow: str | None):
        called["hit"] = (run_id, workflow)
        clear_pending_decision(run_id, experiment_dir=exp)  # advanced → marker cleared
        return type("R", (), {"action": "advanced", "brief": None})(), 0

    monkeypatch.setattr(guard, "_run_drive_tick", _fake_tick)
    out = guard.build_hook_output(_stop_payload(tmp_path))

    assert called["hit"] == (_RUN_ID, _WORKFLOW)
    assert out is not None
    assert "decision" not in out
    assert "advanced the driver in code" in out["systemMessage"]


def test_completer_breaker_open_degrades_to_rejector(tmp_path: Path, monkeypatch) -> None:
    """The fork-exhaustion night: transport breaker OPEN → the completer REFUSES
    and the output is byte-identical to today's rejector; the tick never runs."""
    _activate(monkeypatch)
    _park_mechanical(tmp_path)
    _commit_y_targeting(tmp_path, _MECH_NEXT_VERB)
    _open_breaker("h")  # the run record's ssh_target is "u@h"

    def _must_not_run(*_a, **_k):
        raise AssertionError("the tick must not run against an open breaker")

    monkeypatch.setattr(guard, "_run_drive_tick", _must_not_run)
    out = guard.build_hook_output(_stop_payload(tmp_path))

    # Byte-identical to the pure rejector.
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND", raising=False)
    assert out == guard.build_hook_output(_stop_payload(tmp_path))
    assert out is not None and out["decision"] == "block" and "systemMessage" not in out


def test_completer_judgment_verb_degrades_to_rejector(tmp_path: Path, monkeypatch) -> None:
    """A JUDGMENT next verb (a recovery arm, not a chain block) → the completer
    refuses and bounces; the model must author the resume."""
    _activate(monkeypatch)
    upsert_run(tmp_path, _record())
    mark_pending_decision(
        _RUN_ID,
        block="submit-s2",
        workflow=_WORKFLOW,
        brief=_BRIEF,
        resume_cursor={"workflow": _WORKFLOW, "run_id": _RUN_ID, "next_verb": "retarget-run"},
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=tmp_path,
    )
    _commit_y_targeting(tmp_path, "retarget-run")

    def _must_not_run(*_a, **_k):
        raise AssertionError("a judgment verb must not run the tick")

    monkeypatch.setattr(guard, "_run_drive_tick", _must_not_run)
    out = guard.build_hook_output(_stop_payload(tmp_path))
    assert out is not None and out["decision"] == "block" and "systemMessage" not in out


def test_completer_new_boundary_reparks_bounces_with_brief(tmp_path: Path, monkeypatch) -> None:
    """The tick advances but re-parks at a NEW boundary (a fresh human decision):
    render-a-proposal is model judgment → bounce carrying the fresh brief."""
    _activate(monkeypatch)
    _park_mechanical(tmp_path)
    _commit_y_targeting(tmp_path, _MECH_NEXT_VERB)

    fresh_brief = {"proposal": "s3 wants a bigger budget", "cost": 99}

    def _fake_tick(exp: Path, run_id: str, workflow: str | None):
        clear_pending_decision(run_id, experiment_dir=exp)
        wf = workflow or _WORKFLOW
        mark_pending_decision(  # re-park at a NEW boundary (submit-s4)
            run_id,
            block=_MECH_NEXT_VERB,
            workflow=wf,
            brief=fresh_brief,
            resume_cursor={"workflow": wf, "run_id": run_id, "next_verb": "submit-s4"},
            awaiting_since="2026-07-03T02:00:00+00:00",
            experiment_dir=exp,
        )
        return type("R", (), {"action": "advanced", "brief": fresh_brief})(), 0

    monkeypatch.setattr(guard, "_run_drive_tick", _fake_tick)
    out = guard.build_hook_output(_stop_payload(tmp_path))
    assert out is not None
    assert out["decision"] == "block"
    assert "submit-s4" in out["reason"]
    assert "bigger budget" in out["reason"]  # the fresh brief rode the reason


def test_completer_same_boundary_repark_proceeds(tmp_path: Path, monkeypatch) -> None:
    """The tick ran but did not advance (same boundary re-parked — not_ready /
    first-span failure): a model bounce buys nothing (finding 21) → PROCEED."""
    _activate(monkeypatch)
    _park_mechanical(tmp_path)
    _commit_y_targeting(tmp_path, _MECH_NEXT_VERB)

    def _fake_tick(exp: Path, run_id: str, workflow: str | None):
        # Leave the marker exactly as parked (the block did not advance).
        return type("R", (), {"action": "skip", "brief": None})(), 1

    monkeypatch.setattr(guard, "_run_drive_tick", _fake_tick)
    out = guard.build_hook_output(_stop_payload(tmp_path))
    assert out is not None
    assert "decision" not in out
    assert "did not advance" in out["systemMessage"]


def test_completer_stop_hook_active_never_runs_tick(tmp_path: Path, monkeypatch) -> None:
    """Loop safety: a forced continuation passes through BEFORE the completer,
    so the tick never runs on a stop_hook_active re-entry."""
    _activate(monkeypatch)
    _park_mechanical(tmp_path)
    _commit_y_targeting(tmp_path, _MECH_NEXT_VERB)

    def _must_not_run(*_a, **_k):
        raise AssertionError("stop_hook_active must short-circuit before the tick")

    monkeypatch.setattr(guard, "_run_drive_tick", _must_not_run)
    assert guard.build_hook_output(_stop_payload(tmp_path, stop_hook_active=True)) is None
