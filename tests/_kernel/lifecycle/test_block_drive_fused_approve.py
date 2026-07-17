"""L1 fused commit+advance (``block-drive --approve``) — unit 4a.

The fused path removes the agent's mechanical SECOND call: instead of
``append-decision`` (journal the ``y``) THEN ``block-drive`` (advance the driver),
ONE ``block-drive`` tick carrying an ``approve`` payload journals the greenlight
through the ONE ``append_decision`` definition (Row 19 — every gate fires
identically) and advances the driver in the same tick, returning the next parked
brief.

These tests exercise the fusion end-to-end against a REAL decision journal +
REAL pending-decision marker, faking only the block-verb subprocess so no cluster
is touched. The Row-19 fire tests prove the fused path refuses whatever a
standalone ``append-decision`` refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._kernel.lifecycle import block_drive as bd
from hpc_agent._kernel.lifecycle.block_drive import run_tick
from hpc_agent.state.decision_journal import append_decision, read_decisions
from hpc_agent.state.journal import mark_pending_decision, read_pending_decision, upsert_run
from hpc_agent.state.run_record import RunRecord

_RUN_ID = "ml_run_fused01"
_WORKFLOW = "submit"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


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
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status="in_flight",
    )


def _park_s2_boundary(exp: Path, run_id: str = _RUN_ID) -> None:
    """Upsert a run and park it at the submit-s2 → submit-s3 greenlight boundary."""
    upsert_run(exp, _record(run_id))
    mark_pending_decision(
        run_id,
        block="submit-s2",
        workflow=_WORKFLOW,
        brief={"proposal": "canary verified", "cost": 42},
        resume_cursor={
            "workflow": _WORKFLOW,
            "run_id": run_id,
            "next_verb": "submit-s3",
            "current_verb": "submit-s2",
            # The spec submit-s2 ran under; a plain-``y`` greenlight whose ``resolved``
            # matches it (minus metadata) is an UNCHANGED spec → an ``advance``.
            "input_spec": {},
        },
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=exp,
    )


def _approve_payload(run_id: str = _RUN_ID, **resolved_overrides: object) -> dict[str, object]:
    # A plain greenlight: ``resolved`` names the successor and nothing else, so it
    # matches the parked ``input_spec`` (minus metadata) → the driver ADVANCES.
    resolved: dict[str, object] = {"next_block": "submit-s3"}
    resolved.update(resolved_overrides)
    return {
        "scope_kind": "run",
        "scope_id": run_id,
        "block": "submit-s2",
        "response": "y",
        "resolved": resolved,
    }


def _fake_s3_parks_at_s4(monkeypatch: pytest.MonkeyPatch, brief: dict[str, object]) -> None:
    """Fake the block-verb subprocess so submit-s3 parks at a NEW submit-s4 boundary."""

    def _fake_run(verb: str, spec: dict, experiment_dir: Path) -> tuple[dict, int]:
        if verb == "submit-s3":
            return {
                "block": "s3",
                "stage_reached": "submitted",
                "needs_decision": True,
                "run_id": _RUN_ID,
                "brief": brief,
                "next_block": {"verb": "submit-s4", "spec_hint": {"run_id": _RUN_ID}},
            }, 0
        return {}, 1

    monkeypatch.setattr(bd, "_run_block_verb", _fake_run)


# ── the headline: one --approve call journals AND advances ────────────────────


def test_one_approve_call_journals_and_returns_next_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ONE fused call journals the ``y`` (byte-compatible with append-decision) AND
    advances the driver, returning the NEXT boundary's parked brief."""
    _park_s2_boundary(tmp_path)
    fresh = {"proposal": "launch the 4-task main array", "cost": 99}
    _fake_s3_parks_at_s4(monkeypatch, fresh)

    result, code = run_tick(
        tmp_path, run_id=_RUN_ID, workflow=_WORKFLOW, approve=_approve_payload()
    )

    assert code == 0
    # The greenlight was journaled through the ONE append_decision definition.
    records = read_decisions(tmp_path, "run", _RUN_ID)
    assert len(records) == 1
    assert records[0]["response"] == "y"
    assert records[0]["block"] == "submit-s2"
    # The driver ADVANCED into submit-s3 and parked at the NEXT boundary, carrying
    # that boundary's fresh brief back to the caller.
    assert result.action == "awaiting_decision"
    assert result.current_verb == "submit-s3"
    assert result.next_verb == "submit-s4"
    # The next boundary's brief rode back (a park may fold a materialized-spec
    # disclosure key alongside it — assert the proposal survived, not exact equality).
    assert result.brief is not None
    assert result.brief["proposal"] == fresh["proposal"]
    assert result.brief["cost"] == fresh["cost"]


def test_fused_advance_clears_the_old_marker_so_the_guard_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the fused advance the rendezvous Stop guard is SILENT on the fused
    path — the committed ``y`` targets the OLD (consumed) boundary, and the new
    marker is a fresh un-answered park."""
    from hpc_agent._kernel.hooks import decision_rendezvous_stop_guard as guard

    _park_s2_boundary(tmp_path)
    _fake_s3_parks_at_s4(monkeypatch, {"proposal": "main array", "cost": 99})
    run_tick(tmp_path, run_id=_RUN_ID, workflow=_WORKFLOW, approve=_approve_payload())

    # The marker now parks at submit-s3 → submit-s4 (a fresh boundary), and the only
    # committed ``y`` targets the already-consumed submit-s3 boundary → guard silent.
    marker = read_pending_decision(_RUN_ID, experiment_dir=tmp_path)
    assert marker["resume_cursor"]["next_verb"] == "submit-s4"
    payload = {"hook_event_name": "Stop", "stop_hook_active": False, "cwd": str(tmp_path)}
    assert guard.build_hook_output(payload) is None


# ── Row 20: the demoted guard STILL fires on a committed-but-UNADVANCED y ──────


def test_backstop_fires_on_unfused_committed_y(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DEMOTION did not delete the guard (verify-a-guard-can-fire): a ``y``
    committed WITHOUT the fused advance (the legacy two-call path, or a fused tick
    that failed after the commit) still trips the backstop. Pinned to the
    capability-dark REJECTOR shape (the default landing) so "still fires" is the
    byte-identical committed-but-unadvanced bounce."""
    from hpc_agent._kernel.hooks import decision_rendezvous_stop_guard as guard

    monkeypatch.delenv("HPC_STOP_HOOK_APPEND", raising=False)
    _park_s2_boundary(tmp_path)
    # Journal the y but DO NOT advance the driver (the un-advanced state).
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block="submit-s2",
        response="y",
        resolved={"approved": True, "next_block": "submit-s3"},
    )
    payload = {"hook_event_name": "Stop", "stop_hook_active": False, "cwd": str(tmp_path)}
    out = guard.build_hook_output(payload)
    assert out is not None
    assert out["decision"] == "block"
    assert _RUN_ID in out["reason"]


# ── Row 19: the fused path refuses whatever append-decision refuses ────────────


def test_fused_path_refuses_a_code_derived_field_like_append_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``resolved`` that hand-commits a CODE-DERIVED field is refused on the fused
    path exactly as a standalone append-decision refuses it — SAME error, and the
    driver never advances (the block-verb must not run)."""
    _park_s2_boundary(tmp_path)

    def _must_not_run(*_a: object, **_k: object) -> tuple[dict, int]:
        raise AssertionError("a refused commit must never advance the driver")

    monkeypatch.setattr(bd, "_run_block_verb", _must_not_run)

    bad = _approve_payload(job_env={"K": "v"})  # job_env is CODE-DERIVED

    # Standalone append-decision refuses it …
    from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
    from hpc_agent.ops.decision.journal import append_decision as journal_append

    with pytest.raises(errors.SpecInvalid, match="CODE-DERIVED"):
        journal_append(experiment_dir=tmp_path, spec=AppendDecisionInput.model_validate(bad))

    # … and the FUSED path refuses it identically.
    with pytest.raises(errors.SpecInvalid, match="CODE-DERIVED"):
        run_tick(tmp_path, run_id=_RUN_ID, workflow=_WORKFLOW, approve=bad)


def test_fused_path_refuses_a_malformed_payload(tmp_path: Path) -> None:
    """A payload that does not validate against AppendDecisionInput is refused as a
    SpecInvalid (mirroring the CLI's ``--spec`` validation)."""
    _park_s2_boundary(tmp_path)
    # Missing the required ``scope_kind`` / ``block`` / ``response``.
    with pytest.raises(errors.SpecInvalid, match="not a valid append-decision spec"):
        run_tick(tmp_path, run_id=_RUN_ID, workflow=_WORKFLOW, approve={"scope_id": _RUN_ID})


def test_dry_run_does_not_journal_the_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry-run fused tick is a preview: it neither journals nor advances."""
    _park_s2_boundary(tmp_path)
    monkeypatch.setattr(
        bd, "_run_block_verb", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no run"))
    )
    result, code = run_tick(
        tmp_path, run_id=_RUN_ID, workflow=_WORKFLOW, dry_run=True, approve=_approve_payload()
    )
    assert code == 0
    assert read_decisions(tmp_path, "run", _RUN_ID) == []
