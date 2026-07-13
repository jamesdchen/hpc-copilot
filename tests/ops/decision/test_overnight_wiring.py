"""Overnight standing-consent WIRING — the item-8 call-site seams (notebook-audit.md).

The substrate (``ops/overnight.py``) is tested in ``test_overnight_consent.py``;
this file pins the three SEAMS that wire it into its call sites:

* **seam 1 (auto-advance under consent)** — ``consume_boundary_under_consent`` +
  ``block_gate.assert_greenlit_or_consented`` + the ``block_drive._chain`` gated
  park: a LIVE consent for a NAMED boundary consumes the greenlight and records
  the auto-advance in the same breath; every not-live / not-named condition parks;
* **seam 2 (morning brief in the snapshot)** — ``status-snapshot`` folds
  ``overnight_morning_brief`` into its digest when a consent/ledger exists;
* **seam 3 (disclosure outlives the consent)** — the consumption list still
  surfaces after the consent has expired.

TOY VOCABULARY ONLY: widget runs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._kernel.lifecycle import block_drive as bd
from hpc_agent.infra.time import utcnow
from hpc_agent.ops import block_gate, overnight
from hpc_agent.ops.status_blocks import status_snapshot
from hpc_agent.state import decision_journal as sdj

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "widget-run-1"
_CMD_SHA = "a3f2c9d1beef00112233"


def _iso(dt: Any) -> str:
    return str(dt.isoformat(timespec="seconds"))


@pytest.fixture
def experiment_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    exp = tmp_path / "exp"
    exp.mkdir()
    return exp


def _resolved(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "expires_at": _iso(utcnow() + timedelta(hours=8)),
        "budget_cap": 50.0,
        "walltime_cap": 3600,
        "cmd_sha": _CMD_SHA,
        "wake": {"kind": "status-watch", "run_id": _RUN_ID},
    }
    base.update(overrides)
    return base


def _seed_consent(
    experiment_dir: Path,
    *,
    scope_kind: str = "run",
    scope_id: str = _RUN_ID,
    resolved: dict[str, Any] | None = None,
) -> None:
    """Write a consent record via the state writer (bypasses the record-time gate).

    So a consumption-time state the record-time gate forbids (e.g. an already-past
    expiry) can be constructed to test the consumption predicate in isolation.
    """
    sdj.append_decision(
        experiment_dir,
        scope_kind=scope_kind,
        scope_id=scope_id,
        block=overnight.OVERNIGHT_CONSENT_BLOCK,
        response="let it run overnight to the widget canary, cap 50 dollars",
        resolved=_resolved() if resolved is None else resolved,
    )


# ── seam 1: consume_boundary_under_consent ────────────────────────────────────


def test_live_consent_consumes_named_boundary_and_records(experiment_dir: Path) -> None:
    _seed_consent(experiment_dir)
    outcome = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        boundary_block="submit-s3",
        current_cmd_sha=_CMD_SHA,
    )
    assert outcome.consumed is True
    assert outcome.decision.reason == "live"
    # The auto-advance was ledgered IN THE SAME BREATH (no unrecorded consumption).
    ledger = overnight.read_consumption_ledger(experiment_dir, "run", _RUN_ID)
    assert len(ledger) == 1
    assert ledger[0]["consumed_block"] == "submit-s3"
    assert ledger[0]["detail"]["cmd_sha"] == _CMD_SHA


def test_boundary_not_named_in_scope_never_consumes(experiment_dir: Path) -> None:
    """A live consent for the run NEVER auto-advances a boundary it does not name."""
    _seed_consent(experiment_dir)
    outcome = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        boundary_block="submit-s2",  # canary — NOT overnight-consumable
        current_cmd_sha=_CMD_SHA,
    )
    assert outcome.consumed is False
    assert outcome.decision.reason == "boundary-not-consumable"
    assert overnight.read_consumption_ledger(experiment_dir, "run", _RUN_ID) == []


@pytest.mark.parametrize(
    ("resolved", "cmd_sha", "expected_reason"),
    [
        (None, "deadbeef99887766", "spec-changed"),
        ({"expires_at": None}, _CMD_SHA, "expired"),
    ],
)
def test_not_live_consent_parks_with_reason(
    experiment_dir: Path,
    resolved: dict[str, Any] | None,
    cmd_sha: str,
    expected_reason: str,
) -> None:
    if resolved is not None and resolved.get("expires_at") is None:
        resolved = _resolved(expires_at=_iso(utcnow() - timedelta(minutes=5)))
    _seed_consent(experiment_dir, resolved=resolved)
    outcome = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        boundary_block="submit-s3",
        current_cmd_sha=cmd_sha,
    )
    assert outcome.consumed is False
    assert outcome.decision.reason == expected_reason
    assert overnight.read_consumption_ledger(experiment_dir, "run", _RUN_ID) == []


def test_over_budget_cap_parks(experiment_dir: Path) -> None:
    _seed_consent(experiment_dir, resolved=_resolved(budget_cap=10.0))
    outcome = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        boundary_block="submit-s3",
        current_cmd_sha=_CMD_SHA,
        spent_budget=25.0,
    )
    assert outcome.consumed is False
    assert outcome.decision.reason == "over-budget-cap"


def test_no_consent_parks(experiment_dir: Path) -> None:
    outcome = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        boundary_block="submit-s3",
        current_cmd_sha=_CMD_SHA,
    )
    assert outcome.consumed is False
    assert outcome.decision.reason == "no-consent"


def test_consumption_is_idempotent_per_identity(experiment_dir: Path) -> None:
    """A re-tick / gate-replay re-enters the boundary but never double-ledgers it."""
    _seed_consent(experiment_dir)
    first = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        boundary_block="submit-s3",
        current_cmd_sha=_CMD_SHA,
    )
    second = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        boundary_block="submit-s3",
        current_cmd_sha=_CMD_SHA,
    )
    assert first.consumed and second.consumed
    assert first.line is not None and second.line is None  # second was a no-op record
    assert len(overnight.read_consumption_ledger(experiment_dir, "run", _RUN_ID)) == 1


# ── seam 1: block_gate.assert_greenlit_or_consented ───────────────────────────


def _journal_greenlight(experiment_dir: Path, verb: str) -> None:
    sdj.append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        block="s2",
        response="y",
        resolved={"next_block": verb},
    )


def test_gate_passes_on_a_journaled_greenlight(experiment_dir: Path) -> None:
    _journal_greenlight(experiment_dir, "submit-s3")
    out = block_gate.assert_greenlit_or_consented(
        experiment_dir,
        run_id=_RUN_ID,
        verb="submit-s3",
        predecessor="S2",
        current_cmd_sha=_CMD_SHA,
    )
    assert out is None  # a human greenlight — the normal path


def test_gate_passes_on_a_live_consent_and_records(experiment_dir: Path) -> None:
    _seed_consent(experiment_dir)
    out = block_gate.assert_greenlit_or_consented(
        experiment_dir,
        run_id=_RUN_ID,
        verb="submit-s3",
        predecessor="S2",
        current_cmd_sha=_CMD_SHA,
    )
    assert out is not None and out.consumed is True
    assert len(overnight.read_consumption_ledger(experiment_dir, "run", _RUN_ID)) == 1


def test_gate_raises_when_neither_greenlight_nor_live_consent(experiment_dir: Path) -> None:
    _seed_consent(
        experiment_dir, resolved=_resolved(expires_at=_iso(utcnow() - timedelta(minutes=5)))
    )
    with pytest.raises(errors.SpecInvalid, match="expired"):
        block_gate.assert_greenlit_or_consented(
            experiment_dir,
            run_id=_RUN_ID,
            verb="submit-s3",
            predecessor="S2",
            current_cmd_sha=_CMD_SHA,
        )


# ── seam 1: the driver's gated-park site ──────────────────────────────────────


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"ran": [], "parked": [], "results": {}}

    def _fake_run(verb: str, spec: dict[str, Any], experiment_dir: Path) -> tuple[dict, int]:
        state["ran"].append(verb)
        return dict(state["results"].get(verb, {})), 0

    monkeypatch.setattr(bd, "_run_block_verb", _fake_run)
    monkeypatch.setattr(
        bd,
        "mark_pending_decision",
        lambda run_id, **kw: state["parked"].append({"run_id": run_id, **kw}),
    )
    import hpc_agent._kernel.lifecycle.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_stamp_driver_tick", lambda *_a, **_k: None)
    return state


_S2_TO_S3 = {
    "submit-s2": {
        "block": "s2",
        "stage_reached": "canary_verified",
        "needs_decision": False,
        "run_id": _RUN_ID,
        "next_block": {"verb": "submit-s3", "spec_hint": {"submit": {"run_id": _RUN_ID}}},
    },
    "submit-s3": {
        "block": "s3",
        "stage_reached": "complete",
        "needs_decision": False,
        "run_id": _RUN_ID,
        "next_block": None,
    },
}


def test_driver_auto_advances_the_gated_boundary_under_live_consent(
    driver: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    driver["results"] = _S2_TO_S3
    monkeypatch.setattr(
        bd,
        "_consume_overnight",
        lambda *_a, **_k: overnight.ConsumptionOutcome(
            True, overnight.ConsentDecision(True, "live", {}), {"consumed_block": "submit-s3"}
        ),
    )
    result, code = bd._chain(
        tmp_path,
        run_id=_RUN_ID,
        workflow="submit",
        first_verb="submit-s2",
        first_spec={},
        first_label="advanced",
    )
    assert code == 0
    # The driver CHAINED into the gated submit-s3 instead of parking.
    assert driver["ran"] == ["submit-s2", "submit-s3"]
    assert driver["parked"] == []
    assert result.action == "terminal"


def test_driver_parks_the_gated_boundary_when_consent_not_live(
    driver: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    driver["results"] = _S2_TO_S3
    monkeypatch.setattr(
        bd,
        "_consume_overnight",
        lambda *_a, **_k: overnight.ConsumptionOutcome(
            False, overnight.ConsentDecision(False, "expired", None), None
        ),
    )
    result, code = bd._chain(
        tmp_path,
        run_id=_RUN_ID,
        workflow="submit",
        first_verb="submit-s2",
        first_spec={},
        first_label="advanced",
    )
    assert code == 0
    assert driver["ran"] == ["submit-s2"]  # submit-s3 did NOT run
    assert result.action == "awaiting_decision"
    assert result.next_verb == "submit-s3"
    # The park brief names WHY the overnight consent did not carry.
    assert "expired" in (result.reason or "")


# ── seams 2+3: status-snapshot fold + disclosure outlives the consent ─────────


def _mk_run_record(exp: Path, run_id: str) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        exp,
        RunRecord(
            run_id=run_id,
            profile="prof",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/scratch/r",
            job_name="job",
            job_ids=["1"],
            total_tasks=10,
            submitted_at="2026-07-06T00:00:00+00:00",
            experiment_dir=str(exp),
            status="in_flight",
        ),
    )


def test_snapshot_folds_the_morning_brief_when_a_consent_exists(experiment_dir: Path) -> None:
    from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec

    _mk_run_record(experiment_dir, _RUN_ID)
    _seed_consent(experiment_dir)
    result = status_snapshot(
        experiment_dir, spec=StatusSnapshotSpec(run_id=_RUN_ID, mark_seen=False)
    )
    overnight_section = result.brief["overnight"]
    assert len(overnight_section) == 1
    assert overnight_section[0]["scope_id"] == _RUN_ID
    assert overnight_section[0]["has_consent"] is True


def test_snapshot_disclosure_survives_consent_expiry(experiment_dir: Path) -> None:
    """A consent that EXPIRED overnight still discloses what it consumed (seam 3)."""
    from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec

    _mk_run_record(experiment_dir, _RUN_ID)
    # An EXPIRED consent, plus a consumption that was ledgered while it was live.
    _seed_consent(
        experiment_dir, resolved=_resolved(expires_at=_iso(utcnow() - timedelta(hours=1)))
    )
    overnight.record_consumption(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        consumed_block="submit-s3",
        event_kind="auto-advance",
        failed_at=_iso(utcnow() - timedelta(hours=3)),
        notification=overnight.notification_plan(experiment_dir),
    )
    result = status_snapshot(
        experiment_dir, spec=StatusSnapshotSpec(run_id=_RUN_ID, mark_seen=False)
    )
    section = result.brief["overnight"]
    assert len(section) == 1
    # The consumption still surfaces even though the consent has lapsed.
    assert section[0]["consumed_count"] == 1
    assert section[0]["consumed"][0]["consumed_block"] == "submit-s3"
    assert section[0]["consumed"][0]["latency_seconds"] > 0


def test_snapshot_overnight_section_is_empty_and_additive_when_nothing_overnight(
    experiment_dir: Path,
) -> None:
    from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec

    _mk_run_record(experiment_dir, _RUN_ID)
    result = status_snapshot(
        experiment_dir, spec=StatusSnapshotSpec(run_id=_RUN_ID, mark_seen=False)
    )
    assert result.brief["overnight"] == []


# ── seam 1: the campaign anomaly boundary ─────────────────────────────────────


_CAMPAIGN_ID = "widget-campaign"
_MANIFEST = {"goal": "sweep widgets", "budget": 100.0, "strategy": {"name": "grid"}}


def _campaign_identity() -> str:
    from hpc_agent.meta.campaign.blocks import _campaign_spec_identity

    return _campaign_spec_identity(_MANIFEST)


@pytest.fixture
def campaign_anomaly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub campaign-advance to a loud-fail anomaly + the manifest read."""
    import hpc_agent.meta.campaign.atoms.advance as advance_mod
    import hpc_agent.meta.campaign.manifest as manifest_mod

    monkeypatch.setattr(
        advance_mod,
        "campaign_advance",
        lambda **_k: {"decision": "stop_circuit_breaker", "reason": "3 loud fails"},
    )
    monkeypatch.setattr(manifest_mod, "read_manifest", lambda *_a, **_k: dict(_MANIFEST))


def test_campaign_anomaly_auto_advances_under_live_consent(
    experiment_dir: Path, campaign_anomaly: None
) -> None:
    from hpc_agent._wire.workflows.campaign_blocks import CampaignWatchSpec
    from hpc_agent.meta.campaign.blocks import campaign_watch

    _seed_consent(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        resolved=_resolved(cmd_sha=_campaign_identity()),
    )
    result = campaign_watch(experiment_dir, spec=CampaignWatchSpec(campaign_id=_CAMPAIGN_ID))
    assert result.needs_decision is False
    assert result.stage_reached == "watching_healthy"
    assert result.brief["overnight_auto_advanced"]["anomaly"] == "stop_circuit_breaker"
    ledger = overnight.read_consumption_ledger(experiment_dir, "campaign", _CAMPAIGN_ID)
    assert len(ledger) == 1 and ledger[0]["consumed_block"] == "campaign-watch"


def test_campaign_anomaly_parks_without_a_live_consent(
    experiment_dir: Path, campaign_anomaly: None
) -> None:
    from hpc_agent._wire.workflows.campaign_blocks import CampaignWatchSpec
    from hpc_agent.meta.campaign.blocks import campaign_watch

    result = campaign_watch(experiment_dir, spec=CampaignWatchSpec(campaign_id=_CAMPAIGN_ID))
    assert result.needs_decision is True
    assert result.stage_reached == "watching_anomaly"
    assert result.brief["overnight_refusal"] == "no-consent"
