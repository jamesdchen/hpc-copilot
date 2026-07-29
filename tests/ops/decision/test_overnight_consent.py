"""Overnight standing-consent — the item-8 substrate (notebook-audit.md).

Fires each leg of the ``overnight-consent`` authorship gate
(``ops/decision/journal.py::_assert_overnight_consent_authorship``) and the
consumption / morning-brief seams (``ops/overnight.py``):

* the consent authorship tiers (re-ruled 2026-07-27 with the MCP elicitation
  popup's retirement): a BOUND record from a binding surface passes; a
  TOKEN-EXACT chat utterance naming the boundary, every declared heal class,
  and the ``cmd_sha`` by an 8+ hex prefix passes; a word-overlap utterance that
  names none of them never does, and a bound record for a different
  boundary/class/expiry is refused;
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


def _seed_bound(
    experiment_dir: Path,
    *,
    scope_kind: str = "run",
    scope_id: str = _RUN_ID,
    heal_classes: list[str] | None = None,
    cmd_sha: str | None = _CMD_SHA,
    expires_at: str | None = None,
    text: str = "let it run overnight to the widget canary, cap 50 dollars",
) -> None:
    """Seed a BOUND overnight-consent utterance (a binding capture surface's write).

    The bound tier: the gate accepts a consent when a view-aware surface (a
    conforming second harness — core ships none since the elicitation popup
    retired) captured the typed reply BOUND to the coverage. The ``channel``
    value is opaque to the gate; it matches scope/block/subject only.
    """
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    bound = {
        "channel": "second-harness",
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "block": overnight.OVERNIGHT_CONSENT_BLOCK,
        "subject": {
            "heal_classes": sorted(heal_classes or []),
            "expires_at": expires_at or _iso(utcnow() + timedelta(hours=8)),
            "cmd_sha": cmd_sha,
        },
    }
    append_utterance(experiment_dir, text, bound=bound)


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


def test_consent_records_when_bound_and_wake_armed(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    result = _append(experiment_dir)
    records = sdj.read_decisions(experiment_dir, "run", _RUN_ID)
    assert result.count == 1
    assert records[-1]["block"] == overnight.OVERNIGHT_CONSENT_BLOCK
    assert records[-1]["resolved"]["cmd_sha"] == _CMD_SHA


# ── consent authorship: bound tier + token-exact chat tier (2026-07-27) ───────


def test_no_bound_record_refused_with_coverage_brief(experiment_dir: Path) -> None:
    # No binding surface captured a consent and nothing in chat names the
    # coverage → refuse, rendering the coverage INLINE (the code-rendered brief
    # the human reads in chat) and naming the type-it-in-chat grant path.
    _arm_wake(_RUN_ID)
    with pytest.raises(errors.SpecInvalid, match="bound consent record") as exc:
        _append(experiment_dir)
    message = str(exc.value)
    assert "To GRANT: type the consent yourself" in message
    assert _RUN_ID in message  # the boundary is rendered inline
    assert _CMD_SHA in message  # the spec identity the sha-prefix leg derives from


def test_bare_ack_refused(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    with pytest.raises(errors.SpecInvalid, match="authorship"):
        _append(experiment_dir, response="y")


def test_word_overlap_utterance_never_satisfies(experiment_dir: Path) -> None:
    # The deleted forensic word-overlap tier stays deleted: a chat utterance
    # whose words overlap the consent text but name neither the boundary nor
    # the cmd_sha prefix can NEVER satisfy the gate.
    _arm_wake(_RUN_ID)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(experiment_dir, "let it run overnight to the widget canary, cap 50 dollars")
    with pytest.raises(errors.SpecInvalid, match="bound consent record"):
        _append(experiment_dir)


def test_token_exact_chat_consent_accepted(experiment_dir: Path) -> None:
    # The chat tier (2026-07-27): a typed utterance naming the boundary
    # token-exactly AND the cmd_sha by an 8+ hex prefix (derivable only from the
    # rendered coverage brief) grants the consent — no binding surface needed.
    _arm_wake(_RUN_ID)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(
        experiment_dir,
        f"I consent to {_RUN_ID} advancing overnight under spec {_CMD_SHA[:8]}, caps accepted",
    )
    result = _append(experiment_dir)
    assert result.count == 1


def test_chat_consent_without_sha_prefix_refused(experiment_dir: Path) -> None:
    # Naming the boundary alone is not enough when the consent binds a cmd_sha:
    # the sha prefix is the vocabulary-impossibility leg.
    _arm_wake(_RUN_ID)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(experiment_dir, f"I consent to {_RUN_ID} advancing overnight, caps accepted")
    with pytest.raises(errors.SpecInvalid, match="bound consent record"):
        _append(experiment_dir)


def test_chat_consent_must_name_declared_heal_classes(experiment_dir: Path) -> None:
    # Declared heal classes must each be named token-exactly in the typed consent.
    _arm_wake(_RUN_ID)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(
        experiment_dir,
        f"I consent to {_RUN_ID} advancing overnight under spec {_CMD_SHA[:8]}",
    )
    with pytest.raises(errors.SpecInvalid, match="bound consent record"):
        _append(experiment_dir, resolved=_resolved(heal_classes=["env_pin"]))


def test_chat_consent_naming_heal_classes_accepted(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(
        experiment_dir,
        f"I consent to {_RUN_ID} advancing overnight under spec {_CMD_SHA[:8]}, "
        "env_pin repairs authorized",
    )
    result = _append(experiment_dir, resolved=_resolved(heal_classes=["env_pin"]))
    assert result.count == 1


def test_bound_record_for_different_boundary_refused(experiment_dir: Path) -> None:
    # A bound record for a DIFFERENT spec identity (cmd_sha) does not cover this one.
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir, cmd_sha="deadbeefcafe99887766")
    with pytest.raises(errors.SpecInvalid, match="bound consent record"):
        _append(experiment_dir)


def test_bound_record_missing_declared_class_refused(experiment_dir: Path) -> None:
    # The consent declares heal_classes the bound record does NOT cover → refuse.
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir, heal_classes=["A"])
    with pytest.raises(errors.SpecInvalid, match="bound consent record"):
        _append(experiment_dir, resolved=_resolved(heal_classes=["A", "B"]))


def test_expired_bound_coverage_refused(experiment_dir: Path) -> None:
    # A bound record whose coverage window has passed no longer covers → refuse.
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir, expires_at=_iso(utcnow() - timedelta(minutes=5)))
    with pytest.raises(errors.SpecInvalid, match="bound consent record"):
        _append(experiment_dir)


def test_bare_bound_text_refused(experiment_dir: Path) -> None:
    # A bound record whose TEXT is a bare ack is still not a deliberate statement.
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir, text="y")
    with pytest.raises(errors.SpecInvalid, match="bound consent record"):
        _append(experiment_dir)


def test_bound_record_for_firing_boundary_accepted(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    result = _append(experiment_dir)
    assert result.count == 1


def test_bound_superset_of_declared_classes_accepted(experiment_dir: Path) -> None:
    # The bound record may cover MORE classes than the consent declares.
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir, heal_classes=["A", "B"])
    result = _append(experiment_dir, resolved=_resolved(heal_classes=["A"]))
    assert result.count == 1


# ── hard caps + spec identity (pins b + c) ────────────────────────────────────


def test_missing_expires_at_composed(experiment_dir: Path) -> None:
    # Poka-yoke: an omitted expires_at is COMPOSED (next-morning boundary) and
    # DISCLOSED, not refused — the record lands.
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    resolved = _resolved()
    del resolved["expires_at"]
    result = _append(experiment_dir, resolved=resolved)
    rec = sdj.read_decisions(experiment_dir, "run", _RUN_ID)[-1]
    assert result.count == 1
    assert "expires_at" in rec["resolved"]["composed_defaults"]
    # The composed boundary is in the future (else the caps gate would refuse it).
    composed_expiry = overnight.parse_iso_utc_or_none(rec["resolved"]["expires_at"])
    assert composed_expiry is not None and composed_expiry > utcnow()


def test_already_expired_at_record_time_refused(experiment_dir: Path) -> None:
    # A human-SUPPLIED past expiry is NOT overridden — the caps gate still fires
    # (composition fills omissions, never masks a bad value).
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    resolved = _resolved(expires_at=_iso(utcnow() - timedelta(hours=1)))
    with pytest.raises(errors.SpecInvalid, match="future"):
        _append(experiment_dir, resolved=resolved)


def test_no_resource_cap_composed(experiment_dir: Path) -> None:
    # Poka-yoke: neither cap present → a walltime_cap is COMPOSED + disclosed.
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    resolved = _resolved()
    del resolved["budget_cap"]
    del resolved["walltime_cap"]
    result = _append(experiment_dir, resolved=resolved)
    rec = sdj.read_decisions(experiment_dir, "run", _RUN_ID)[-1]
    assert result.count == 1
    assert "walltime_cap" in rec["resolved"]["composed_defaults"]
    assert rec["resolved"]["walltime_cap"] > 0


def test_missing_cmd_sha_binding_refused(experiment_dir: Path) -> None:
    _arm_wake(_RUN_ID)
    # Bound record with no cmd_sha matches the cmd_sha-less consent → bound
    # authorship passes and the STRUCTURAL caps refusal on the missing identity fires.
    _seed_bound(experiment_dir, cmd_sha=None)
    resolved = _resolved()
    del resolved["cmd_sha"]
    with pytest.raises(errors.SpecInvalid, match="cmd_sha"):
        _append(experiment_dir, resolved=resolved)


# ── the wake leg (second amendment) ───────────────────────────────────────────


def test_wake_not_armed_auto_armed(experiment_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Poka-yoke: no armed watch → the write path ARMS it (composes the detach) and
    # records, instead of refusing. The real arm spawns a detached worker; stub it
    # to write the lease (a successful spawn) so status_watch_armed then reads True.
    def _fake_arm(_ed: Path, run_id: str) -> bool:
        _arm_wake(run_id)
        return True

    monkeypatch.setattr(overnight, "_arm_status_watch", _fake_arm)
    _seed_bound(experiment_dir)
    result = _append(experiment_dir)  # no lease pre-created
    assert result.count == 1
    assert overnight.status_watch_armed(_RUN_ID) is True


def test_wake_token_absent_composed(experiment_dir: Path) -> None:
    # Poka-yoke: an omitted wake TOKEN is composed (the watch is already armed here)
    # and disclosed — the record lands rather than refusing.
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    resolved = _resolved()
    del resolved["wake"]
    result = _append(experiment_dir, resolved=resolved)
    rec = sdj.read_decisions(experiment_dir, "run", _RUN_ID)[-1]
    assert result.count == 1
    assert rec["resolved"]["wake"]["kind"] == "status-watch"
    assert "wake" in rec["resolved"]["composed_defaults"]


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


# ── the gates behind the poka-yoke STILL FIRE (never-fires assertions) ─────────
#
# The conversions moved the caps/wake refusals off the append path (composition
# satisfies them), but the underlying assertions MUST still fire when constructed
# directly — "verify a guard can actually fire" (engineering-principles). These
# call the gate functions with a block that skipped composition.


def test_assert_consent_hard_caps_still_fires_missing_expires() -> None:
    resolved = _resolved()
    del resolved["expires_at"]
    with pytest.raises(errors.SpecInvalid, match="expires_at"):
        overnight.assert_consent_hard_caps(resolved)


def test_assert_consent_hard_caps_still_fires_no_cap() -> None:
    resolved = _resolved()
    del resolved["budget_cap"]
    del resolved["walltime_cap"]
    with pytest.raises(errors.SpecInvalid, match="resource ceiling"):
        overnight.assert_consent_hard_caps(resolved)


def test_assert_consent_hard_caps_still_fires_missing_cmd_sha() -> None:
    resolved = _resolved()
    del resolved["cmd_sha"]
    with pytest.raises(errors.SpecInvalid, match="cmd_sha"):
        overnight.assert_consent_hard_caps(resolved)


def test_assert_wake_armed_still_fires_missing_token(experiment_dir: Path) -> None:
    resolved = _resolved()
    del resolved["wake"]
    with pytest.raises(errors.SpecInvalid, match="wake"):
        overnight.assert_wake_armed(
            experiment_dir, scope_kind="run", scope_id=_RUN_ID, resolved=resolved
        )


def test_assert_wake_armed_still_fires_unarmed_run(experiment_dir: Path) -> None:
    # Wake token present, but no armed/live status-watch lease → the run-scope leg
    # of the assertion fires.
    with pytest.raises(errors.SpecInvalid, match="status-watch"):
        overnight.assert_wake_armed(
            experiment_dir, scope_kind="run", scope_id=_RUN_ID, resolved=_resolved()
        )


# ── compose unit tests (the poka-yoke helpers in isolation) ───────────────────


def test_compose_fills_expires_and_walltime_and_discloses() -> None:
    out = overnight.compose_consent_defaults({"cmd_sha": _CMD_SHA})
    assert "expires_at" in out and "walltime_cap" in out
    assert set(out["composed_defaults"]) == {"expires_at", "walltime_cap"}
    # The walltime cap is sized to the overnight window (positive, bounded).
    assert out["walltime_cap"] > 0


def test_compose_never_composes_cmd_sha() -> None:
    out = overnight.compose_consent_defaults({})
    assert "cmd_sha" not in out  # the identity binding is never defaulted


def test_compose_does_not_override_supplied_values() -> None:
    supplied_expiry = _iso(utcnow() + timedelta(hours=6))
    out = overnight.compose_consent_defaults(
        {"expires_at": supplied_expiry, "budget_cap": 12.0, "cmd_sha": _CMD_SHA}
    )
    assert out["expires_at"] == supplied_expiry  # untouched
    assert "walltime_cap" not in out  # a budget cap already satisfies the ceiling
    assert "composed_defaults" not in out  # nothing composed → no disclosure key


def test_compose_wake_token_for_campaign_scope_no_arm(
    monkeypatch: pytest.MonkeyPatch, experiment_dir: Path
) -> None:
    # A campaign scope composes the wake TOKEN but never arms a per-run watch.
    def _boom(_ed: Path, _rid: str) -> bool:
        raise AssertionError("campaign scope must not arm a per-run status-watch")

    monkeypatch.setattr(overnight, "_arm_status_watch", _boom)
    out = overnight.arm_consent_wake(
        experiment_dir, scope_kind="campaign", scope_id="widget-campaign", resolved={}
    )
    assert out["wake"] == {"kind": "status-watch", "campaign_id": "widget-campaign"}
    assert "wake" in out["composed_defaults"]


# ── S1: the placement leg (run-queue plan §10.S1) ────────────────────────────


def test_placement_outside_the_bound_set_kills_consent(experiment_dir: Path) -> None:
    """A consent bound to hoffman2 must refuse a boundary re-placed to carc."""
    _seed_consent_raw(experiment_dir, _resolved(placement="hoffman2"))
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
    )
    assert decision.live is False
    assert decision.reason == "placement-changed"


def test_placement_inside_the_bound_set_stays_live(experiment_dir: Path) -> None:
    _seed_consent_raw(experiment_dir, _resolved(placement=["carc", "hoffman2"]))
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
    )
    assert decision.live is True
    assert decision.reason == "live"


def test_placement_blind_consent_survives_a_placed_boundary(experiment_dir: Path) -> None:
    """The additive-on-upgrade guarantee: a pre-migration consent (no
    ``placement`` in its resolved dict) must stay live even when the caller
    KNOWS the boundary's cluster — absent recorded disables the leg."""
    _seed_consent_raw(experiment_dir, _resolved())  # no placement key
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
    )
    assert decision.live is True


def test_placement_bound_consent_survives_an_unknown_current(experiment_dir: Path) -> None:
    """A pre-stamp sidecar (caller passes None) must not kill a bound consent —
    the symmetric half of absent-disables."""
    _seed_consent_raw(experiment_dir, _resolved(placement="hoffman2"))
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
    )
    assert decision.live is True


def test_placement_leg_orders_after_spec_leg(experiment_dir: Path) -> None:
    """When BOTH spec and placement moved, the reason names the spec — the
    first failing leg wins, matching the documented leg order."""
    _seed_consent_raw(experiment_dir, _resolved(placement="hoffman2"))
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha="deadbeef99887766",
        current_placement="carc",
    )
    assert decision.live is False
    assert decision.reason == "spec-changed"


def _pin_clusters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    """Point HPC_CLUSTERS_CONFIG at a temp yaml declaring exactly *keys* — the
    record-time placement gate validates keys against the loaded config, so a
    placement-bound test must pin the vocabulary it uses."""
    path = tmp_path / "consent-clusters.yaml"
    path.write_text("".join(f"{k}:\n  host: {k}.example.edu\n" for k in keys), encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(path))


def test_refusal_renders_a_paste_ready_grant_line_that_grants(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal's copy-paste line is not decoration — pasted verbatim into
    chat, it must carry every token the chat tier reads (boundary, classes,
    sha prefix, cluster set) and therefore grant. A rendered line that fails
    its own gate would be worse than no line at all."""
    _pin_clusters(tmp_path, monkeypatch, "carc", "hoffman2")
    _arm_wake(_RUN_ID)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    resolved = _resolved(placement=["carc", "hoffman2"], heal_classes=["env_pin"])
    with pytest.raises(errors.SpecInvalid, match="copy-paste") as exc:
        _append(experiment_dir, resolved=resolved)
    paste_line = str(exc.value).splitlines()[-1].strip()
    assert _RUN_ID in paste_line
    assert "carc" in paste_line and "hoffman2" in paste_line
    assert "env_pin" in paste_line
    assert _CMD_SHA[:12] in paste_line

    append_utterance(experiment_dir, paste_line)
    result = _append(experiment_dir, resolved=resolved)
    assert result.count == 1


def _write_sidecar(experiment_dir: Path, *, cluster: str | None = None) -> None:
    """A real run sidecar carrying the CURRENT identity (`cmd_sha=_CMD_SHA`) —
    the token the park-time / re-grant offers derive their sha clause from."""
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
        hpc_agent_version="0.2.0",
        submitted_at="2026-07-29T00:00:00+00:00",
        executor="python3 src/run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="1" * 64,
        cluster=cluster,
    )


def test_offered_park_grant_line_round_trips_through_the_authorship_gate(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The park-time OFFER (block_drive's answer menu — the 'speculative y done
    right') renders through the SAME one-home grant vocabulary as the refusal
    above (``ops.overnight.render_grant_line``), so the line offered at the
    run's FIRST park — before any refusal was ever shown — pasted verbatim into
    chat must satisfy the chat tier for a consent whose resolved binds the same
    cmd_sha + cluster. An offered line that failed its own gate would be worse
    than no offer at all."""
    from hpc_agent._kernel.lifecycle import block_drive as bd

    _pin_clusters(tmp_path, monkeypatch, "hoffman2")
    _arm_wake(_RUN_ID)
    _write_sidecar(experiment_dir, cluster="hoffman2")
    offer = bd._compose_standing_offer_for_park(
        experiment_dir, run_id=_RUN_ID, workflow="submit", is_anomaly_terminator=False
    )
    assert offer is not None
    # The offered line carries the tokens the gate reads: boundary, 8+ hex sha
    # prefix of the SIDECAR identity, and the cluster stamp — no heal classes
    # (the minimal watcher-re-arm-only form; code invents no classes).
    assert _RUN_ID in offer["grant_line"]
    assert _CMD_SHA[:12] in offer["grant_line"]
    assert "hoffman2" in offer["grant_line"]
    assert "repair classes" not in offer["grant_line"]

    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(experiment_dir, offer["grant_line"])
    result = _append(experiment_dir, resolved=_resolved(placement="hoffman2"))
    assert result.count == 1


# ── the RE-GRANT offer: consent death by spec change costs one paste ──────────

_OLD_SHA = "beefc0dedead00112233"  # the PRE-EDIT tree fingerprint a dead consent binds


def test_regrant_offer_renders_the_dead_consents_coverage_for_the_current_spec(
    experiment_dir: Path,
) -> None:
    """A consent killed by a cmd_sha move is re-offered with ITS OWN declared
    coverage (classes + placement) re-bound to the CURRENT identity — the same
    one-home sentence, so the morning-after re-grant is one paste."""
    _write_sidecar(experiment_dir, cluster="hoffman2")
    _seed_consent_raw(
        experiment_dir,
        _resolved(cmd_sha=_OLD_SHA, heal_classes=["env_pin"], placement="hoffman2"),
    )
    offer = overnight.regrant_offer(experiment_dir, scope_kind="run", scope_id=_RUN_ID)
    assert offer is not None
    assert offer["reason"] == "spec-changed"
    assert offer["previous_cmd_sha"] == _OLD_SHA
    assert offer["cmd_sha"] == _CMD_SHA
    line = offer["grant_line"]
    assert _CMD_SHA[:12] in line  # re-bound to the CURRENT spec …
    assert _OLD_SHA[:12] not in line  # … never the dead one
    assert "env_pin" in line and "hoffman2" in line  # the human's own coverage
    # The note says honestly what pasting does: authorship only, nothing auto.
    assert "AUTHORSHIP" in offer["note"]
    assert "auto-answered" in offer["note"]


def test_regrant_offer_only_fires_on_the_spec_changed_death(experiment_dir: Path) -> None:
    """NEGATIVE legs: a LIVE consent, an EXPIRED one (a fresh decision, not a
    re-grant), and NO consent at all each offer nothing."""
    _write_sidecar(experiment_dir)
    # No consent recorded → nothing to re-grant.
    assert overnight.regrant_offer(experiment_dir, scope_kind="run", scope_id=_RUN_ID) is None
    # Expired (checked before the sha leg) → not the spec-changed class.
    _seed_consent_raw(
        experiment_dir, _resolved(cmd_sha=_OLD_SHA, expires_at=_iso(utcnow() - timedelta(hours=1)))
    )
    assert overnight.regrant_offer(experiment_dir, scope_kind="run", scope_id=_RUN_ID) is None
    # Live (same sha, unexpired) → nothing left to grant.
    _seed_consent_raw(experiment_dir, _resolved())
    assert overnight.regrant_offer(experiment_dir, scope_kind="run", scope_id=_RUN_ID) is None


def test_morning_brief_carries_the_regrant_offer_when_consent_died_on_spec_change(
    experiment_dir: Path,
) -> None:
    """The surface that reports the dead consent hands over the fresh line: the
    morning brief's ``regrant_offer`` names the current sha so the human never
    reconstructs the grant grammar from a 'spec-changed' reason string."""
    _write_sidecar(experiment_dir, cluster="hoffman2")
    _seed_consent_raw(experiment_dir, _resolved(cmd_sha=_OLD_SHA, placement="hoffman2"))
    brief = overnight.overnight_morning_brief(experiment_dir, scope_kind="run", scope_id=_RUN_ID)
    assert brief["has_consent"] is True
    regrant = brief["regrant_offer"]
    assert regrant is not None
    assert regrant["reason"] == "spec-changed"
    assert _CMD_SHA[:12] in regrant["grant_line"]
    assert "hoffman2" in regrant["grant_line"]


def test_morning_brief_regrant_offer_is_none_for_a_live_consent(experiment_dir: Path) -> None:
    """NEGATIVE: a live consent's brief is byte-unchanged but for the None field."""
    _write_sidecar(experiment_dir)
    _seed_consent_raw(experiment_dir, _resolved())
    brief = overnight.overnight_morning_brief(experiment_dir, scope_kind="run", scope_id=_RUN_ID)
    assert brief["regrant_offer"] is None


def test_regrant_offer_line_round_trips_through_the_authorship_gate(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-offered line, pasted verbatim, must GRANT the replacement consent
    that re-binds the same coverage to the new sha — the same fire-proof bar as
    the refusal's line and the first-park offer (a re-grant line that failed its
    own gate would lose the night twice)."""
    _pin_clusters(tmp_path, monkeypatch, "hoffman2")
    _arm_wake(_RUN_ID)
    _write_sidecar(experiment_dir, cluster="hoffman2")
    _seed_consent_raw(
        experiment_dir,
        _resolved(cmd_sha=_OLD_SHA, heal_classes=["env_pin"], placement="hoffman2"),
    )
    offer = overnight.regrant_offer(experiment_dir, scope_kind="run", scope_id=_RUN_ID)
    assert offer is not None

    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(experiment_dir, offer["grant_line"])
    result = _append(
        experiment_dir,
        resolved=_resolved(heal_classes=["env_pin"], placement="hoffman2"),
    )
    # The seeded dead consent plus the freshly granted replacement.
    assert result.count == 2
    records = sdj.read_decisions(experiment_dir, "run", _RUN_ID)
    assert records[-1]["resolved"]["cmd_sha"] == _CMD_SHA


# ── S1 record-time placement gate: strict where the human is awake ────────────


def test_malformed_placement_shape_refused_at_record_time(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present-but-broken placement must refuse at grant time, not silently
    record a consent the consumption leg can never fire on (absent-disables
    would treat it as placement-blind — not what the human meant)."""
    _pin_clusters(tmp_path, monkeypatch, "hoffman2")
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    with pytest.raises(errors.SpecInvalid, match="placement gate.*unusable"):
        _append(experiment_dir, resolved=_resolved(placement={"cluster": "hoffman2"}))


def test_unknown_placement_key_refused_with_near_miss(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd key would mismatch EVERY real placement — a guaranteed 3am
    placement-changed park. The gate catches it now, with the suggestion."""
    _pin_clusters(tmp_path, monkeypatch, "hoffman2", "carc")
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    with pytest.raises(errors.SpecInvalid, match="did you mean 'hoffman2'"):
        _append(experiment_dir, resolved=_resolved(placement="hoffman"))


def test_valid_placement_passes_the_record_time_gate(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_clusters(tmp_path, monkeypatch, "hoffman2")
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    result = _append(experiment_dir, resolved=_resolved(placement="hoffman2"))
    assert result.count == 1


def test_placement_blind_consent_still_records(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No placement field ⇒ the gate is silent — every pre-migration consent
    shape must keep recording unchanged."""
    _pin_clusters(tmp_path, monkeypatch, "hoffman2")
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    assert _append(experiment_dir).count == 1


# ── S1.4 campaign placement_scope: composed from the campaign's own runs ──────

_CAMPAIGN_ID = "widget_sweep_over_time_hoffman2"


def _mk_campaign_run(
    exp: Path,
    run_id: str,
    cluster: str,
    *,
    campaign_id: str = _CAMPAIGN_ID,
    submitted_at: str = "2026-07-28T00:00:00+00:00",
) -> None:
    """A journal record for a campaign iteration placed on *cluster*."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        exp,
        RunRecord(
            run_id=run_id,
            profile="widget",
            cluster=cluster,
            ssh_target="u@h",
            remote_path="/scratch/widget",
            job_name="widget",
            job_ids=["1"],
            total_tasks=4,
            submitted_at=submitted_at,
            experiment_dir=str(exp),
            campaign_id=campaign_id,
            status="in_flight",
        ),
    )


def test_campaign_consent_composes_placement_and_discloses_it(experiment_dir: Path) -> None:
    """A campaign consent must record the cluster set the S1 leg compares against
    (§10.S1.4) — composed, not typed, and disclosed AS composed."""
    _mk_campaign_run(experiment_dir, "widget-iter-1", "hoffman2")
    out = overnight.compose_overnight_consent(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        resolved={},
    )
    assert out["placement"] == ["hoffman2"]
    assert "placement" in out["composed_defaults"]


def test_multi_cluster_campaign_scope_covers_every_cluster_it_spans(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A campaign spanning two clusters records BOTH — membership over a set is
    what keeps a dynamic split from parking on every placement swing.

    Pins the vocabulary the campaign actually spans: the composer will not
    compose a key the ACTIVE config does not define (that would hand the
    record-time gate a value it must refuse — see
    ``test_composer_declines_a_placement_its_own_gate_would_refuse``), and
    ``carc`` is not in the packaged default.
    """
    _pin_clusters(tmp_path, monkeypatch, "carc", "hoffman2")
    _mk_campaign_run(experiment_dir, "widget-iter-1", "hoffman2")
    _mk_campaign_run(experiment_dir, "widget-iter-2", "carc")
    assert overnight.campaign_placement_scope(experiment_dir, _CAMPAIGN_ID) == [
        "carc",
        "hoffman2",
    ]
    out = overnight.compose_overnight_consent(
        experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID, resolved={}
    )
    assert out["placement"] == ["carc", "hoffman2"]


def test_composer_declines_a_placement_its_own_gate_would_refuse(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A campaign on a cluster key the ACTIVE config no longer defines must stay
    grantable — placement-blind, not un-recordable.

    ``_assert_placement_wellformed`` validates COMPOSED keys exactly as it
    validates typed ones, so a composer that emits a retired or renamed key
    makes the consent impossible to record at all: ``SpecInvalid`` naming a
    field the human never typed and cannot edit, whose stated remedy ("fix the
    shape or drop the field") is unactionable. Reproduced against the shipped
    code with ``ebm_all_buckets_carc`` on a host whose config defines only
    hoffman2/discovery.

    ALL-OR-NOTHING rather than filtered to the survivors: recording only the
    known half would judge a campaign whose newest run sits on the unknown key
    against a set that cannot contain it, and consumption would park at 3am —
    the false-kill this leg forbids. Composing nothing leaves the leg off, which
    is the safe direction and the documented pre-migration shape.
    """
    _pin_clusters(tmp_path, monkeypatch, "hoffman2")
    _mk_campaign_run(experiment_dir, "widget-iter-1", "carc")
    assert overnight.campaign_placement_scope(experiment_dir, _CAMPAIGN_ID) == ["carc"]

    out = overnight.compose_overnight_consent(
        experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID, resolved={}
    )
    assert "placement" not in out
    assert "placement" not in out.get("composed_defaults", [])
    # And the record-time gate — the thing that used to refuse — now passes it.
    overnight._assert_placement_wellformed(out.get("placement"))


def test_a_typed_placement_naming_an_unknown_key_is_still_refused(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composer's narrowing must not soften the gate for a value a HUMAN typed.

    Composed-and-declined is "we could not prove where this runs"; typed-and-wrong
    is a typo the human is awake to fix, and it keeps failing loudly.
    """
    _pin_clusters(tmp_path, monkeypatch, "hoffman2")
    _mk_campaign_run(experiment_dir, "widget-iter-1", "carc")
    out = overnight.compose_overnight_consent(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        resolved={"placement": ["carc"]},
    )
    assert out["placement"] == ["carc"]
    assert "placement" not in out.get("composed_defaults", [])
    with pytest.raises(errors.SpecInvalid, match="absent from clusters.yaml"):
        overnight._assert_placement_wellformed(out["placement"])


def test_campaign_placement_is_never_derived_by_splitting_the_campaign_id(
    experiment_dir: Path,
) -> None:
    """``compose_campaign_id`` composes ``<base>_<cluster>`` and is NEVER parsed:
    bases contain underscores, so a split is a guess. Here the id's suffix says
    hoffman2 while the campaign's runs are on carc — the runs win."""
    _mk_campaign_run(experiment_dir, "widget-iter-1", "carc")
    assert overnight.campaign_placement_scope(experiment_dir, _CAMPAIGN_ID) == ["carc"]
    assert overnight.campaign_current_placement(experiment_dir, _CAMPAIGN_ID) == "carc"


def test_current_placement_follows_the_newest_iteration(experiment_dir: Path) -> None:
    """A campaign that MOVED reports the cluster it runs on NOW — the drift the
    consumption leg exists to catch."""
    _mk_campaign_run(
        experiment_dir, "widget-iter-1", "hoffman2", submitted_at="2026-07-01T00:00:00+00:00"
    )
    _mk_campaign_run(
        experiment_dir, "widget-iter-2", "carc", submitted_at="2026-07-28T00:00:00+00:00"
    )
    assert overnight.campaign_current_placement(experiment_dir, _CAMPAIGN_ID) == "carc"


def test_compose_leaves_a_supplied_campaign_placement_untouched(experiment_dir: Path) -> None:
    """A supplied placement is the human's binding, right or wrong: composition
    must not overwrite it (a malformed one must still reach the record-time gate)."""
    _mk_campaign_run(experiment_dir, "widget-iter-1", "hoffman2")
    out = overnight.compose_consent_placement(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        resolved={"placement": {"cluster": "carc"}},
    )
    assert out["placement"] == {"cluster": "carc"}  # untouched, gate's business
    assert "composed_defaults" not in out


def test_compose_never_composes_placement_for_a_run_scope(experiment_dir: Path) -> None:
    """D7 is the CAMPAIGN half. A run scope's placement is the boundary caller's
    sidecar read; composing one here would newly force every run consent's chat
    grant to name a cluster — a widening this phase did not decide."""
    _mk_campaign_run(experiment_dir, "widget-iter-1", "hoffman2")
    out = overnight.compose_consent_placement(
        experiment_dir, scope_kind="run", scope_id=_RUN_ID, resolved={}
    )
    assert "placement" not in out


def test_compose_composes_nothing_when_the_campaign_has_no_placed_run(
    experiment_dir: Path,
) -> None:
    """Nothing to prove ⇒ nothing bound: a guessed binding is the false-kill
    direction, and a placement-blind consent is the documented shape."""
    out = overnight.compose_consent_placement(
        experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID, resolved={}
    )
    assert "placement" not in out
    assert overnight.campaign_current_placement(experiment_dir, _CAMPAIGN_ID) is None


def test_composed_campaign_placement_makes_the_chat_grant_name_the_cluster(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composed set is not decoration: §10.S1.5's authorship leg now bites for
    campaign consents — the human must say WHERE out loud. The refusal renders the
    cluster and the paste-ready grant line carrying it grants."""
    from hpc_agent.meta.campaign.blocks import campaign_spec_identity
    from hpc_agent.meta.campaign.manifest import read_manifest

    cid = "widget-sweep-2"
    _pin_clusters(tmp_path, monkeypatch, "hoffman2", "carc")
    _mk_campaign_run(experiment_dir, "widget-iter-1", "hoffman2", campaign_id=cid)
    identity = campaign_spec_identity(read_manifest(experiment_dir, campaign_id=cid))
    resolved = {
        "expires_at": _iso(utcnow() + timedelta(hours=8)),
        "budget_cap": 50.0,
        "cmd_sha": identity,
    }
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    # Names the boundary and the sha prefix — but not the cluster the campaign runs on.
    append_utterance(
        experiment_dir,
        f"I grant overnight consent for campaign {cid} under spec {identity[:12]}",
    )
    with pytest.raises(errors.SpecInvalid, match="cluster set") as exc:
        _append(experiment_dir, scope_kind="campaign", scope_id=cid, resolved=dict(resolved))
    paste_line = str(exc.value).splitlines()[-1].strip()
    assert "hoffman2" in paste_line

    append_utterance(experiment_dir, paste_line)
    result = _append(experiment_dir, scope_kind="campaign", scope_id=cid, resolved=dict(resolved))
    assert result.count == 1
    records = sdj.read_decisions(experiment_dir, "campaign", cid)
    # The binding the consumption leg compares is ON the record, marked composed.
    assert records[-1]["resolved"]["placement"] == ["hoffman2"]
    assert "placement" in records[-1]["resolved"]["composed_defaults"]


def test_campaign_scope_consent_refuses_a_boundary_off_its_cluster_set(
    experiment_dir: Path,
) -> None:
    """The consumption half at CAMPAIGN scope: bound to hoffman2, boundary on carc
    ⇒ placement-changed (the same predicate the run scope already fires on)."""
    sdj.append_decision(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        block=overnight.OVERNIGHT_CONSENT_BLOCK,
        response="let the widget campaign self-chain overnight, cap 50 dollars",
        resolved=_resolved(placement=["hoffman2"]),
    )
    refused = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
    )
    assert refused.live is False
    assert refused.reason == "placement-changed"
    live = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="hoffman2",
    )
    assert live.live is True


# ── the {cluster: cap} vocabulary (run-queue plan §3, Phase 2) ────────────────


def _seed_spend(
    experiment_dir: Path, *, cluster: str | None, budget: float = 0.0, walltime: float = 0.0
) -> None:
    """Ledger one consumption line carrying spend, optionally cluster-stamped."""
    detail: dict[str, Any] = {"cmd_sha": _CMD_SHA}
    if budget:
        detail["spent_budget"] = budget
    if walltime:
        detail["spent_walltime"] = walltime
    if cluster:
        detail["cluster"] = cluster
    overnight.record_consumption(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        consumed_block="submit-s3",
        event_kind="auto-advance",
        failed_at=_iso(utcnow()),
        detail=detail,
    )


def test_per_cluster_budget_cap_fires_on_that_clusters_spend(experiment_dir: Path) -> None:
    """A {cluster: cap} consent refuses consumption on the cluster whose metered
    spend exceeds its own cap — reason names the per-cluster leg."""
    _seed_consent_raw(experiment_dir, _resolved(placement={"carc": {"budget_cap": 10.0}}))
    _seed_spend(experiment_dir, cluster="carc", budget=25.0)
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
    )
    assert decision.live is False
    assert decision.reason == "over-cluster-budget-cap"


def test_per_cluster_walltime_cap_fires(experiment_dir: Path) -> None:
    _seed_consent_raw(experiment_dir, _resolved(placement={"carc": {"walltime_cap": 100.0}}))
    _seed_spend(experiment_dir, cluster="carc", walltime=250.0)
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
    )
    assert decision.live is False
    assert decision.reason == "over-cluster-walltime-cap"


def test_another_clusters_spend_never_counts(experiment_dir: Path) -> None:
    """The per-cluster meter splits by detail.cluster: hoffman2's spend must not
    consume carc's cap."""
    _seed_consent_raw(
        experiment_dir,
        _resolved(placement={"carc": {"budget_cap": 10.0}, "hoffman2": {"budget_cap": 10.0}}),
    )
    _seed_spend(experiment_dir, cluster="hoffman2", budget=25.0)
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
    )
    assert decision.live is True
    assert decision.reason == "live"


def test_unstamped_legacy_spend_counts_toward_no_cluster(experiment_dir: Path) -> None:
    """Ledger lines predating the cluster stamp count globally but toward no
    per-cluster total — undercounting is the tolerable direction for a cap
    that did not exist when they were written."""
    _seed_consent_raw(experiment_dir, _resolved(placement={"carc": {"budget_cap": 10.0}}))
    _seed_spend(experiment_dir, cluster=None, budget=25.0)
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
    )
    assert decision.live is True


def test_unknown_current_placement_keeps_the_cap_legs_off(experiment_dir: Path) -> None:
    """The S1 absent-disables direction applies to the cap legs too: an unknown
    boundary cluster is not evidence of overspend at 3am."""
    _seed_consent_raw(experiment_dir, _resolved(placement={"carc": {"budget_cap": 10.0}}))
    _seed_spend(experiment_dir, cluster="carc", budget=25.0)
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
    )
    # No current_placement: membership leg off AND cap legs off.
    assert decision.live is True


def test_global_cap_leg_orders_before_the_per_cluster_leg(experiment_dir: Path) -> None:
    """When both the global and the per-cluster budget cap are blown, the reason
    names the global leg — the documented leg order."""
    _seed_consent_raw(
        experiment_dir,
        _resolved(budget_cap=5.0, placement={"carc": {"budget_cap": 10.0}}),
    )
    _seed_spend(experiment_dir, cluster="carc", budget=25.0)
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
        spent_budget=25.0,
    )
    assert decision.live is False
    assert decision.reason == "over-budget-cap"


def test_consumption_stamps_the_cluster_for_the_meter(experiment_dir: Path) -> None:
    """consume_boundary_under_consent must stamp detail.cluster from the
    boundary's current_placement so the per-cluster meter has data."""
    _seed_consent_raw(experiment_dir, _resolved(placement={"carc": {"budget_cap": 100.0}}))
    outcome = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        boundary_block="submit-s3",
        current_cmd_sha=_CMD_SHA,
        current_placement="carc",
        detail={"spent_budget": 3.0},
    )
    assert outcome.consumed is True
    assert outcome.line is not None
    assert outcome.line["detail"]["cluster"] == "carc"
    assert overnight.consumed_spend(experiment_dir, "run", _RUN_ID, cluster="carc") == (3.0, 0.0)
    assert overnight.consumed_spend(experiment_dir, "run", _RUN_ID, cluster="hoffman2") == (
        0.0,
        0.0,
    )


def test_consumed_spend_cluster_filter_splits_the_totals(experiment_dir: Path) -> None:
    _seed_spend(experiment_dir, cluster="carc", budget=2.0, walltime=10.0)
    _seed_spend(experiment_dir, cluster="hoffman2", budget=5.0)
    _seed_spend(experiment_dir, cluster=None, budget=7.0)
    assert overnight.consumed_spend(experiment_dir, "run", _RUN_ID) == (14.0, 10.0)
    assert overnight.consumed_spend(experiment_dir, "run", _RUN_ID, cluster="carc") == (2.0, 10.0)
    assert overnight.consumed_spend(experiment_dir, "run", _RUN_ID, cluster="hoffman2") == (
        5.0,
        0.0,
    )


# ── {cluster: cap} record-time strictness (the write gate) ───────────────────


def test_unknown_cap_field_refused_at_record_time(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd cap field ("budget" for "budget_cap") would be silently dropped
    at consumption — record time is the only moment it can fail loudly."""
    _pin_clusters(tmp_path, monkeypatch, "carc")
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    with pytest.raises(errors.SpecInvalid, match="unknown cap field"):
        _append(experiment_dir, resolved=_resolved(placement={"carc": {"budget": 10.0}}))


def test_non_positive_cap_value_refused_at_record_time(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_clusters(tmp_path, monkeypatch, "carc")
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    with pytest.raises(errors.SpecInvalid, match="not a positive finite number"):
        _append(experiment_dir, resolved=_resolved(placement={"carc": {"budget_cap": -1}}))


def test_cap_mapping_with_known_keys_records(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_clusters(tmp_path, monkeypatch, "carc", "hoffman2")
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    result = _append(
        experiment_dir,
        resolved=_resolved(placement={"carc": {"budget_cap": 10.0}, "hoffman2": {}}),
    )
    assert result.count == 1


def test_cap_mapping_key_validated_against_clusters_yaml(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map form's keys get the same typo protection as the list form."""
    _pin_clusters(tmp_path, monkeypatch, "carc", "hoffman2")
    _arm_wake(_RUN_ID)
    _seed_bound(experiment_dir)
    with pytest.raises(errors.SpecInvalid, match="did you mean 'hoffman2'"):
        _append(experiment_dir, resolved=_resolved(placement={"hoffman": {"budget_cap": 10.0}}))


def test_fully_capped_map_satisfies_the_ceiling_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A {cluster: cap} placement in which EVERY cluster carries a cap is a
    ceiling — no global cap required."""
    _pin_clusters(tmp_path, monkeypatch, "carc", "hoffman2")
    overnight.assert_consent_hard_caps(
        {
            "expires_at": _iso(utcnow() + timedelta(hours=8)),
            "cmd_sha": _CMD_SHA,
            "placement": {"carc": {"budget_cap": 10.0}, "hoffman2": {"walltime_cap": 60.0}},
        }
    )


def test_partially_capped_map_does_not_satisfy_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One uncapped cluster leaves the consent unbounded there — refused
    without a global cap."""
    _pin_clusters(tmp_path, monkeypatch, "carc", "hoffman2")
    with pytest.raises(errors.SpecInvalid, match="caps gate.*EVERY"):
        overnight.assert_consent_hard_caps(
            {
                "expires_at": _iso(utcnow() + timedelta(hours=8)),
                "cmd_sha": _CMD_SHA,
                "placement": {"carc": {"budget_cap": 10.0}, "hoffman2": {}},
            }
        )


def test_coverage_brief_renders_the_per_cluster_caps(
    experiment_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human must READ the per-cluster ceilings in the refusal's coverage
    render — consenting to a cap they never saw is the gap the inline render
    closes. The caps stay structural: the paste line needs the cluster NAMES
    only, and pasted verbatim it must still grant."""
    _pin_clusters(tmp_path, monkeypatch, "carc")
    _arm_wake(_RUN_ID)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)
    resolved = _resolved(placement={"carc": {"budget_cap": 10.0}})
    with pytest.raises(errors.SpecInvalid, match="carc caps: budget_cap=10.0") as exc:
        _append(experiment_dir, resolved=resolved)
    paste_line = str(exc.value).splitlines()[-1].strip()
    assert "carc" in paste_line
    append_utterance(experiment_dir, paste_line)
    result = _append(experiment_dir, resolved=resolved)
    assert result.count == 1
