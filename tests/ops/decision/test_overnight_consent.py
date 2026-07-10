"""Overnight standing-consent — the item-8 substrate (notebook-audit.md).

Fires each leg of the ``overnight-consent`` authorship gate
(``ops/decision/journal.py::_assert_overnight_consent_authorship``) and the
consumption / morning-brief seams (``ops/overnight.py``):

* the consent is the human's OWN typed utterance — a bare ack is refused, and a
  model-composed utterance (no derivation from the harness log) is refused;
* hard caps ride the record — missing ``expires_at`` / an already-past expiry /
  no resource cap / a missing ``cmd_sha`` binding each refuse;
* the WAKE must be armed — an ``overnight-consent`` whose scope has no live
  ``status-watch`` lease is refused-with-remedy;
* spec-identity binding kills consent on a ``cmd_sha`` change at consumption;
* the morning brief surfaces ``failed_at`` vs ``surfaced_at``.

TOY VOCABULARY ONLY: widget runs, never a real domain's words.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.infra.time import utcnow
from hpc_agent.ops import overnight
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state import decision_journal as sdj
from hpc_agent.state.utterances import append_utterance, utterances_path

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "widget-run-1"
_CMD_SHA = "a3f2c9d1beef00112233"


def _iso(dt: Any) -> str:
    return str(dt.isoformat(timespec="seconds"))


@pytest.fixture
def experiment_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo dir with the journal home redirected under it (HPC_JOURNAL_DIR)."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    exp = tmp_path / "exp"
    exp.mkdir()
    return exp


def _arm_wake(run_id: str) -> None:
    """Create a live detached status-watch lease for *run_id* (the armed wake)."""
    lease = overnight._watch_lease_path(run_id)
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")


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


def _append(
    experiment_dir: Path,
    *,
    response: str = "let it run overnight to the widget canary, cap 50 dollars",
    scope_kind: str = "run",
    scope_id: str = _RUN_ID,
    resolved: dict[str, Any] | None = None,
) -> Any:
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "block": overnight.OVERNIGHT_CONSENT_BLOCK,
            "response": response,
            "resolved": _resolved() if resolved is None else resolved,
        }
    )
    return append_decision(experiment_dir=experiment_dir, spec=spec)


# ── happy path ────────────────────────────────────────────────────────────────


def test_consent_records_when_wake_armed_and_caps_present(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    result = _append(experiment_dir)
    records = sdj.read_decisions(experiment_dir, "run", _RUN_ID)
    assert result.count == 1
    assert records[-1]["block"] == overnight.OVERNIGHT_CONSENT_BLOCK
    assert records[-1]["resolved"]["cmd_sha"] == _CMD_SHA


# ── authorship (pin a) ────────────────────────────────────────────────────────


def test_bare_ack_refused(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    with pytest.raises(errors.SpecInvalid, match="authorship"):
        _append(experiment_dir, response="y")


def test_model_composed_utterance_refused_when_log_present(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    # Seed a harness utterance log whose words do NOT overlap the consent text.
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(experiment_dir, "please schedule the meeting tomorrow")
    with pytest.raises(errors.SpecInvalid, match="logged human utterance"):
        _append(
            experiment_dir,
            response="fabricated overnight authorization the human never typed",
        )


def test_derived_utterance_accepted_when_log_present(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(experiment_dir, "let it run overnight to the widget canary, cap 50 dollars")
    result = _append(experiment_dir)  # default response derives from that utterance
    assert result.count == 1


# ── hard caps + spec identity (pins b + c) ────────────────────────────────────


def test_missing_expires_at_refused(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    resolved = _resolved()
    del resolved["expires_at"]
    with pytest.raises(errors.SpecInvalid, match="expires_at"):
        _append(experiment_dir, resolved=resolved)


def test_already_expired_at_record_time_refused(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    resolved = _resolved(expires_at=_iso(utcnow() - timedelta(hours=1)))
    with pytest.raises(errors.SpecInvalid, match="future"):
        _append(experiment_dir, resolved=resolved)


def test_no_resource_cap_refused(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    resolved = _resolved()
    del resolved["budget_cap"]
    del resolved["walltime_cap"]
    with pytest.raises(errors.SpecInvalid, match="resource ceiling"):
        _append(experiment_dir, resolved=resolved)


def test_missing_cmd_sha_binding_refused(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    resolved = _resolved()
    del resolved["cmd_sha"]
    with pytest.raises(errors.SpecInvalid, match="cmd_sha"):
        _append(experiment_dir, resolved=resolved)


# ── the wake leg (second amendment) ───────────────────────────────────────────


def test_wake_not_armed_refused(experiment_dir: Path) -> None:
    # No lease created → no armed status-watch.
    with pytest.raises(errors.SpecInvalid, match="status-watch"):
        _append(experiment_dir)


def test_wake_token_absent_refused(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    resolved = _resolved()
    del resolved["wake"]
    with pytest.raises(errors.SpecInvalid, match="wake"):
        _append(experiment_dir, resolved=resolved)


# ── block convention ──────────────────────────────────────────────────────────


def test_consent_block_refused_off_run_or_campaign_scope(experiment_dir: Path) -> None:
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "notebook",
            "scope_id": "widget-audit",
            "block": overnight.OVERNIGHT_CONSENT_BLOCK,
            "response": "let it run overnight",
            "resolved": _resolved(),
        }
    )
    with pytest.raises(errors.SpecInvalid, match="standing consent"):
        append_decision(experiment_dir=experiment_dir, spec=spec)


# ── consumption: spec-identity binding + caps + expiry ────────────────────────


def _seed_consent_raw(experiment_dir: Path, resolved: dict[str, Any]) -> None:
    """Write a consent record directly via the state writer (bypass the gate).

    Used to construct consumption-time states the record-time gate forbids
    (e.g. an already-past expiry) so the consumption predicate can be tested in
    isolation.
    """
    sdj.append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        block=overnight.OVERNIGHT_CONSENT_BLOCK,
        response="overnight ok",
        resolved=resolved,
    )


def test_live_consent_status(experiment_dir: Path) -> None:
    _seed_consent_raw(experiment_dir, _resolved())
    decision = overnight.standing_consent_status(
        experiment_dir, scope_kind="run", scope_id=_RUN_ID, current_cmd_sha=_CMD_SHA
    )
    assert decision.live is True
    assert decision.reason == "live"


def test_spec_change_kills_consent(experiment_dir: Path) -> None:
    _seed_consent_raw(experiment_dir, _resolved(cmd_sha=_CMD_SHA))
    decision = overnight.standing_consent_status(
        experiment_dir, scope_kind="run", scope_id=_RUN_ID, current_cmd_sha="deadbeef99887766"
    )
    assert decision.live is False
    assert decision.reason == "spec-changed"


def test_expired_consent_not_live(experiment_dir: Path) -> None:
    _seed_consent_raw(experiment_dir, _resolved(expires_at=_iso(utcnow() - timedelta(minutes=5))))
    decision = overnight.standing_consent_status(
        experiment_dir, scope_kind="run", scope_id=_RUN_ID, current_cmd_sha=_CMD_SHA
    )
    assert decision.live is False
    assert decision.reason == "expired"


def test_over_budget_cap_not_live(experiment_dir: Path) -> None:
    _seed_consent_raw(experiment_dir, _resolved(budget_cap=10.0))
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        spent_budget=25.0,
    )
    assert decision.live is False
    assert decision.reason == "over-budget-cap"


def test_no_consent_status(experiment_dir: Path) -> None:
    decision = overnight.standing_consent_status(
        experiment_dir, scope_kind="run", scope_id=_RUN_ID, current_cmd_sha=_CMD_SHA
    )
    assert decision.live is False
    assert decision.reason == "no-consent"


# ── notification leg + morning brief (pin d + amendment b) ────────────────────


def test_notification_plan_records_gap_without_push_hook(experiment_dir: Path) -> None:
    plan = overnight.notification_plan(experiment_dir)
    # No watchdog alert-delivery hook installed in the test env → gap recorded.
    assert plan["push_available"] is False
    assert plan["gap"]


def test_morning_brief_surfaces_failed_at_vs_surfaced_at(experiment_dir: Path) -> None:
    _seed_consent_raw(experiment_dir, _resolved())
    failed_at = _iso(utcnow() - timedelta(hours=3))
    overnight.record_consumption(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        consumed_block="submit-s3",
        event_kind="anomaly",
        failed_at=failed_at,
        detail={"note": "widget canary died"},
        notification=overnight.notification_plan(experiment_dir),
    )
    brief = overnight.overnight_morning_brief(experiment_dir, scope_kind="run", scope_id=_RUN_ID)
    assert brief["has_consent"] is True
    assert brief["consumed_count"] == 1
    item = brief["consumed"][0]
    assert item["failed_at"] == failed_at
    assert item["surfaced_at"] == brief["surfaced_at"]
    assert item["latency_seconds"] is not None and item["latency_seconds"] > 0
    # The missing push channel means this item's latency was baked in.
    assert item["push_available"] is False
    assert item["disclosure_gap"]
