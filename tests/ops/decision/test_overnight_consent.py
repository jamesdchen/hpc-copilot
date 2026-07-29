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
