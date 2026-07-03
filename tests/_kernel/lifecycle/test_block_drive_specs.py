"""Spec-shape regression tests for the ``block-drive`` driver (block-drive.md §3).

The class the wave-4 unit tests MISSED: they faked the block subprocess, so the
spec the driver builds for each hop was never validated against the successor
block's real pydantic Spec model. This file exercises exactly that seam — for
every chain / advance hop the driver takes, the spec it constructs must
``model_validate`` against the successor block's actual Spec model (the acting
blocks nest their inputs under a required sub-object with ``extra="forbid"``, so a
blind top-level ``run_id`` injection is a ``SpecInvalid`` the instant the driver
crosses an acting boundary).

Also pins the park-before-gated semantics (an in-code chain never journals the
greenlight a gated block's gate requires → the driver parks) and the SoT that
``block_chain.is_gated`` matches the live ``assert_greenlit_target`` callers.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from hpc_agent._kernel.lifecycle import block_drive as bd
from hpc_agent._kernel.lifecycle.block_drive import run_tick
from hpc_agent.infra import block_chain

_RUN_ID = "ml_run_abcd1234"


# ── faked driver harness (block-verb subprocess + journal faked) ──────────────


@pytest.fixture
def faked(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the journal reads/writes + the block-verb subprocess.

    Unlike a spec-blind fake, ``ran`` captures the EXACT spec dict the driver
    built for every span so a test can validate it against the successor block's
    real Spec model.
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

    def _clear(run_id: str, **_k: Any) -> None:
        state["cleared"].append(run_id)

    def _mark(run_id: str, **kw: Any) -> None:
        state["parked"].append({"run_id": run_id, **kw})

    monkeypatch.setattr(bd, "clear_pending_decision", _clear)
    monkeypatch.setattr(bd, "mark_pending_decision", _mark)

    def _fake_run(verb: str, spec: dict[str, Any], experiment_dir: Path) -> tuple[dict, int]:
        state["ran"].append({"verb": verb, "spec": spec})
        return dict(state["results"].get(verb, {})), 0

    monkeypatch.setattr(bd, "_run_block_verb", _fake_run)
    import hpc_agent._kernel.lifecycle.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_stamp_driver_tick", lambda *_a, **_k: None)
    return state


def _pending(
    *, current_verb: str, next_verb: str | None, input_spec: dict[str, Any]
) -> dict[str, Any]:
    return {
        "block": current_verb,
        "workflow": block_chain.WORKFLOW_OF[current_verb],
        "brief": {},
        "resume_cursor": {
            "workflow": block_chain.WORKFLOW_OF[current_verb],
            "run_id": _RUN_ID,
            "next_verb": next_verb,
            "current_verb": current_verb,
            "input_spec": input_spec,
        },
        "awaiting_since": "2026-07-03T00:00:00+00:00",
        "cmd_sha": None,
    }


# ── hop 1: status-snapshot → status-watch (in-code chain, from spec_hint) ──────


def _live_snapshot_next_block(tmp_path: Path) -> dict[str, Any]:
    """Invoke the REAL status_snapshot on a live single run; return its next_block."""
    from types import SimpleNamespace

    from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec
    from hpc_agent.ops import status_blocks

    rec = SimpleNamespace(
        run_id=_RUN_ID,
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        status="in_flight",
        last_status={"running": 4, "pending": 6},
        last_tick_at=None,
        last_seen_by_human_at=None,
        total_tasks=10,
    )
    with (
        mock.patch.object(status_blocks, "load_run", return_value=rec),
        mock.patch.object(status_blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(status_blocks, "mark_seen_by_human"),
    ):
        result = status_blocks.status_snapshot(
            tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID, mark_seen=False)
        )
    assert result.stage_reached == "snapshot_clean"
    assert result.needs_decision is False
    assert result.next_block is not None
    return dict(result.next_block)


def test_live_snapshot_emits_valid_status_watch_spec_hint(tmp_path: Path) -> None:
    """The REAL status_snapshot's spec_hint validates as a StatusWatchSpec.

    This is the exact seam the faked wave-4 tests skipped: the emitted
    ``next_block.spec_hint`` must be a shape ``StatusWatchSpec`` accepts (nested
    ``monitor``), not a bare top-level ``run_id``.
    """
    from hpc_agent._wire.workflows.status_blocks import StatusWatchSpec

    nb = _live_snapshot_next_block(tmp_path)
    assert nb["verb"] == "status-watch"
    assert nb["spec_hint"] == {"monitor": {"run_id": _RUN_ID}}
    # The load-bearing assertion: the hint the driver passes VERBATIM validates.
    StatusWatchSpec.model_validate(nb["spec_hint"])


def test_driver_chains_snapshot_to_watch_with_valid_spec(
    faked: dict[str, Any], tmp_path: Path
) -> None:
    """The driver chains snapshot→watch in code and the spec it builds is valid."""
    from hpc_agent._wire.workflows.status_blocks import StatusWatchSpec

    nb = _live_snapshot_next_block(tmp_path)
    faked["results"] = {
        "status-snapshot": {
            "block": "snapshot",
            "stage_reached": "snapshot_clean",
            "needs_decision": False,
            "run_id": _RUN_ID,
            "next_block": nb,
        },
        # watch parks on its own decision so the tick has a clean stop.
        "status-watch": {
            "block": "watch",
            "stage_reached": "watch_anomaly",
            "needs_decision": True,
            "brief": {},
            "next_block": None,
        },
    }
    run_tick(tmp_path, run_id=_RUN_ID, workflow="status")

    ran = {r["verb"]: r["spec"] for r in faked["ran"]}
    assert "status-watch" in ran
    built = ran["status-watch"]
    # No top-level run_id injection — the spec is the spec_hint verbatim.
    assert built == {"monitor": {"run_id": _RUN_ID}}
    StatusWatchSpec.model_validate(built)  # must not raise (the fixed bug)


# ── hop 2: submit-s1 → submit-s2 resume advance (spec from committed resolved) ─


def _valid_submit_s2_resolved() -> dict[str, Any]:
    """A correctly-shaped SubmitS2Spec dict (the approved ``resolved`` §3)."""
    from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
    from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

    flow = SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=_RUN_ID,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=True,
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )
    spec = SubmitS2Spec(
        submit=SubmitAndVerifySpec(submit=flow, poll_interval_sec=1, wait_budget_sec=5),
        detach=False,
    )
    return spec.model_dump(mode="json")


def test_resume_advance_passes_committed_resolved_verbatim(faked: dict[str, Any]) -> None:
    """s1→s2 advance: the driver runs the approved resolved spec, minus metadata.

    The committed ``resolved`` IS the correctly-shaped SubmitS2Spec — the driver
    must pass it VERBATIM (only stripping the ``next_block`` routing token), never
    inject a top-level ``run_id`` that would violate SubmitS2Spec's ``extra=forbid``.
    """
    from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec

    resolved = _valid_submit_s2_resolved()
    committed = {**resolved, "next_block": "submit-s2"}  # + the routing token
    faked["pending"] = _pending(
        current_verb="submit-s1", next_verb="submit-s2", input_spec=resolved
    )
    faked["committed"] = committed  # unchanged vs input_spec (minus meta) → advance
    faked["results"] = {
        "submit-s2": {
            "block": "s2",
            "stage_reached": "canary_verified",
            "needs_decision": True,
            "next_block": None,
        }
    }
    run_tick(Path("."), run_id=_RUN_ID, workflow="submit")

    ran = {r["verb"]: r["spec"] for r in faked["ran"]}
    assert "submit-s2" in ran
    built = ran["submit-s2"]
    assert "next_block" not in built  # metadata stripped
    assert built == resolved
    SubmitS2Spec.model_validate(built)  # the acting spec validates


# ── hop 3: aggregate-check → aggregate-run PARKS (gated, not chained) ──────────


def test_aggregate_check_parks_before_gated_run(faked: dict[str, Any]) -> None:
    """aggregate-run is greenlight-gated → the driver parks, never chains into it."""
    faked["results"] = {
        "aggregate-check": {
            "block": "check",
            "stage_reached": "ready",
            "needs_decision": False,
            "run_id": _RUN_ID,
            "brief": {"terminal": True},
            "next_block": {
                "verb": "aggregate-run",
                "why": "reduce",
                "spec_hint": {"aggregate": {"run_id": _RUN_ID}},
            },
        }
    }
    result, code = run_tick(Path("."), run_id=_RUN_ID, workflow="aggregate")
    assert code == 0
    assert result.action == "awaiting_decision"
    assert result.next_verb == "aggregate-run"
    assert [r["verb"] for r in faked["ran"]] == ["aggregate-check"]  # run did NOT execute
    assert faked["parked"][0]["resume_cursor"]["next_verb"] == "aggregate-run"


# ── the SoT: is_gated matches the live assert_greenlit_target callers ──────────


def test_is_gated_matches_live_gate_callers() -> None:
    """GATED_BLOCKS is exactly the set of block verbs whose op calls the gate."""
    from hpc_agent.ops import aggregate_blocks, submit_blocks

    # (verb, op function) for every block that could call the greenlight gate.
    candidates: list[tuple[str, Any]] = [
        ("submit-s1", submit_blocks.submit_s1),
        ("submit-s2", submit_blocks.submit_s2),
        ("submit-s3", submit_blocks.submit_s3),
        ("submit-s4", submit_blocks.submit_s4),
        ("aggregate-check", aggregate_blocks.aggregate_check),
        ("aggregate-run", aggregate_blocks.aggregate_run),
    ]
    derived = {
        verb for verb, fn in candidates if "assert_greenlit_target(" in inspect.getsource(fn)
    }
    assert derived == {"submit-s2", "submit-s3", "submit-s4", "aggregate-run"}
    assert set(block_chain.GATED_BLOCKS) == derived
    for verb in derived:
        assert block_chain.is_gated(verb)
    assert not block_chain.is_gated("status-watch")
    assert not block_chain.is_gated("campaign-watch")


# ── campaign in-code chains: spec_hints already validate (ungated) ─────────────


def test_campaign_chain_spec_hints_validate() -> None:
    """The two ungated campaign in-code hops emit specs their successor accepts."""
    from hpc_agent._wire.workflows.campaign_blocks import (
        CampaignCompleteSpec,
        CampaignWatchSpec,
    )

    g2w = block_chain.next_block_hint(
        "campaign-greenlight", "greenlit", why="observe", campaign_id="camp1"
    )
    assert g2w is not None and g2w["verb"] == "campaign-watch"
    CampaignWatchSpec.model_validate(g2w["spec_hint"])

    w2c = block_chain.next_block_hint(
        "campaign-watch", "watching_complete", why="finish", campaign_id="camp1"
    )
    assert w2c is not None and w2c["verb"] == "campaign-complete"
    CampaignCompleteSpec.model_validate(w2c["spec_hint"])
