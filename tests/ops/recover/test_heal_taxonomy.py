"""Overnight-repair A/B/C1/C2 taxonomy — classifier + arms (overnight-repair.md).

Covers, per the build sequencing:

* the classifier FIRES — the §6 worked examples route to their ruled classes
  (fork-exhaustion→A, severed-connection→A, stale-wheel→C2, env-drift→C1);
* the C1 mint — post-`y`, the SAME transport drift classifies Class B;
* the spend-meter gating — a consent's budget cap is enforced against the metered
  running total, not the 0.0 placeholder;
* the never-actuate boundary — the doctor seat ROUTES transport drift without opening
  SSH; the enactment (stray reap) is a SPAWNED detached child;
* the Class-B env-pin restore + verify + boundary-index canary sampling;
* recurrence escalation — a sever recurring under keepalives routes to a C-finding.

TOY VOCABULARY ONLY: widget campaigns / runs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.infra.time import utcnow
from hpc_agent.ops import overnight
from hpc_agent.ops.recover import heal_taxonomy as tax

if TYPE_CHECKING:
    from pathlib import Path

_CAMPAIGN_ID = "widget-campaign"


@pytest.fixture
def experiment_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    exp = tmp_path / "exp"
    exp.mkdir()
    return exp


# ── the classifier FIRES: worked examples → ruled classes ─────────────────────


@pytest.mark.parametrize(
    ("cause", "expected_class", "expected_arm"),
    [
        ("fork-exhaustion", "A", "stray-reap"),
        ("severed-connection", "A", "watcher-rearm"),
        ("stale-wheel", "C2", "report"),
        ("env-drift", "C1", "elicit"),
        ("resource-exhaustion", "C1", "elicit"),
        ("result-anomaly", "C2", "report"),
    ],
)
def test_classifier_routes_worked_examples(
    cause: str, expected_class: str, expected_arm: str
) -> None:
    routing = tax.classify_crash_cause(cause)
    assert routing.heal_class == expected_class
    assert routing.arm == expected_arm
    # Only Class B re-verifies (§7.1); A/C1/C2 do not.
    assert routing.reverify == (expected_class == "B")


def test_unclassified_cause_never_healed() -> None:
    routing = tax.classify_crash_cause("who-knows")
    assert routing.heal_class == ""
    assert routing.arm == "none"
    assert "NEVER healed" in routing.reason


def test_env_drift_becomes_class_b_when_anchored() -> None:
    routing = tax.classify_crash_cause("env-drift", context={"anchored": True})
    assert routing.heal_class == "B"
    assert routing.arm == "env-pin-restore"
    assert routing.reverify is True


# ── the C1 mint: post-`y`, the same drift classifies B ────────────────────────


def test_c1_env_drift_mints_anchor_then_classifies_b(experiment_dir: Path) -> None:
    live = {"HPC_SSH_ENGINE": "asyncssh"}

    # Episode 1: no anchor → Class C1 (elicit-then-mint).
    r1 = tax.env_drift_class(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        live_transport_overrides=live,
    )
    assert r1.heal_class == "C1"
    assert r1.arm == "elicit"
    assert "HPC_SSH_ENGINE" in r1.detail["drift"]

    # The composed elicitation names the predicate + the proposed pin (unset).
    elicit = tax.compose_env_pin_elicitation(
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        cmd_sha="widget-identity",
        live_transport_overrides=live,
    )
    assert elicit["heal_class"] == "C1"
    assert elicit["proposed_pinned_env"] == {"HPC_SSH_ENGINE": None}

    # The human's `y` mints the anchor (unset the drifted var).
    tax.mint_env_pin_anchor(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        pinned_env={"HPC_SSH_ENGINE": None},
        consent_ref="decision-abc",
    )
    assert tax.has_env_pin_anchor(experiment_dir, "campaign", _CAMPAIGN_ID)

    # Episode 2: the SAME drift now classifies Class B (auto-restore + verify).
    r2 = tax.env_drift_class(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        live_transport_overrides=live,
    )
    assert r2.heal_class == "B"
    assert r2.arm == "env-pin-restore"
    assert r2.reverify is True


def test_env_matches_anchor_is_no_heal(experiment_dir: Path) -> None:
    tax.mint_env_pin_anchor(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        pinned_env={"HPC_SSH_ENGINE": None},
    )
    # No live override → env matches the pin → nothing to heal.
    r = tax.env_drift_class(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        live_transport_overrides={},
    )
    assert r.heal_class == ""
    assert r.arm == "none"


def test_mint_refuses_non_transport_var(experiment_dir: Path) -> None:
    from hpc_agent import errors

    with pytest.raises(errors.SpecInvalid):
        tax.mint_env_pin_anchor(
            experiment_dir,
            scope_kind="campaign",
            scope_id=_CAMPAIGN_ID,
            pinned_env={"HPC_RUN_ID": "abc"},  # job-env identity, never an anchor
        )


# ── Class-B env-pin restore + verify + boundary-index canary ──────────────────


def test_restore_env_to_pin_and_verify() -> None:
    anchor = {"kind": "env-pin", "pinned_env": {"HPC_SSH_ENGINE": None}}
    live = {"HPC_SSH_ENGINE": "asyncssh"}

    correction = tax.restore_env_to_pin(anchor, live)
    assert correction == {"HPC_SSH_ENGINE": None}  # unset it

    # After the corrected child dials with the var unset, verify passes.
    verify_ok = tax.verify_env_restored(anchor, {})
    assert verify_ok["verified"] is True
    assert verify_ok["mismatches"] == {}

    # A verify against a still-drifted env FAILS (flips to fail-loud, §7.1).
    verify_bad = tax.verify_env_restored(anchor, {"HPC_SSH_ENGINE": "asyncssh"})
    assert verify_bad["verified"] is False
    assert "HPC_SSH_ENGINE" in verify_bad["mismatches"]


def test_boundary_index_sample_hits_edges() -> None:
    assert tax.boundary_index_sample(0, 99) == [0, 99]
    assert tax.boundary_index_sample(99, 0) == [0, 99]  # order-tolerant
    assert tax.boundary_index_sample(5, 5) == [5]  # single-index range


# ── the spend meter gates the caps (sequencing item 1) ────────────────────────


def _seed_campaign_consent(experiment_dir: Path, resolved: dict[str, Any]) -> None:
    # The STATE-level writer bypasses the ops-level bound-authorship gate (the same
    # path the existing overnight-self-heal tests seed through).
    from hpc_agent.state import decision_journal as sdj

    sdj.append_decision(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        block=overnight.OVERNIGHT_CONSENT_BLOCK,
        response="I consent to overnight widget advances under a budget cap.",
        resolved=resolved,
    )


def test_spend_meter_sums_ledger_and_gates_budget(experiment_dir: Path) -> None:
    # Two consumption lines carrying real spend, summing to 40.0.
    for cost in (25.0, 15.0):
        overnight.record_consumption(
            experiment_dir,
            scope_kind="campaign",
            scope_id=_CAMPAIGN_ID,
            consumed_block="campaign-watch",
            event_kind="auto-advance",
            failed_at=str(utcnow().isoformat(timespec="seconds")),
            detail={"spent_budget": cost, "spent_walltime": cost * 10},
        )
    spent_budget, spent_walltime = overnight.consumed_spend(
        experiment_dir, "campaign", _CAMPAIGN_ID
    )
    assert spent_budget == 40.0
    assert spent_walltime == 400.0

    # Under a budget_cap of 30, the metered total (40) is over cap → not live.
    _seed_campaign_consent(
        experiment_dir,
        {
            "expires_at": str((utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")),
            "budget_cap": 30.0,
            "cmd_sha": "widget-identity",
            "wake": {"kind": "status-watch", "campaign_id": _CAMPAIGN_ID},
            "heal_classes": ["A", "B"],
        },
    )
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        current_cmd_sha="widget-identity",
        spent_budget=spent_budget,
    )
    assert decision.live is False
    assert decision.reason == "over-budget-cap"


# ── declared heal-class cap (§7.2 rule 4) ─────────────────────────────────────


def test_consent_authorizes_declared_classes_only() -> None:
    consent = {"resolved": {"heal_classes": ["A", "B"]}}
    assert overnight.consent_authorizes_class(consent, "A") is True
    assert overnight.consent_authorizes_class(consent, "B") is True
    # A consent that names no classes heals nothing.
    assert overnight.consent_authorizes_class({"resolved": {}}, "A") is False
    # Class C is NEVER authorized as an autonomous heal, even if listed.
    assert overnight.consent_authorizes_class({"resolved": {"heal_classes": ["C1"]}}, "C1") is False
    assert overnight.consent_authorizes_class({"resolved": {"heal_classes": ["C2"]}}, "C2") is False


# ── the never-actuate boundary: doctor ROUTES, children ENACT ─────────────────


def test_doctor_routes_transport_drift_without_ssh(
    experiment_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hpc_agent.infra.remote as remote
    from hpc_agent._wire.queries.doctor import DoctorSpec
    from hpc_agent.ops.recover.doctor import doctor

    # Arm the canonical SSH seam to EXPLODE — the doctor routing must never dial.
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("doctor transport-drift routing opened SSH — never-actuate breach")

    monkeypatch.setattr(remote, "ssh_run", _boom)

    # A live healable transport override.
    monkeypatch.setenv("HPC_SSH_ENGINE", "asyncssh")

    result = doctor(experiment_dir=experiment_dir, spec=DoctorSpec(self_heal=True))
    messages = " ".join(a.get("message", "") for a in result.get("alerts", []))
    assert "transport-env drift routed to Class C1" in messages
    assert "HPC_SSH_ENGINE" in messages
    # doctor never unsets the var — pure routing.
    import os

    assert os.environ.get("HPC_SSH_ENGINE") == "asyncssh"


def test_stray_reap_enactment_is_a_spawned_detached_child(
    experiment_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    import hpc_agent._kernel.lifecycle.detached as detached

    def _fake_spawn(*, run_id: str, block: str, argv: list[str], log_path: Any, cwd: str) -> Any:
        captured.update(run_id=run_id, block=block, argv=argv, cwd=cwd)
        return object()

    monkeypatch.setattr(detached, "_spawn_detached", _fake_spawn)

    # Also ensure the ENACTMENT path never dials from THIS process — the child does.
    import hpc_agent.infra.remote as remote

    monkeypatch.setattr(
        remote,
        "ssh_run",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("parent dialed — child should")),
    )

    tax.spawn_stray_reap_detached(experiment_dir, ssh_target="user@login1", max_age_sec=3900)

    assert captured["block"] == "stray-sweep"
    assert "stray-sweep" in captured["argv"]
    assert "--reap" in captured["argv"]
    assert "user@login1" in captured["argv"]


# ── recurrence escalation (§9 ruling): a recurring sever → C-finding ──────────


def test_recurrence_escalation_routes_to_c_finding(experiment_dir: Path) -> None:
    # Below threshold: a single sever heals normally (Class A) — no escalation.
    assert (
        tax.escalate_if_recurring(experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID)
        is None
    )

    # Journal two Class-A re-arms (respawned, heal_class=A) → the sever recurred.
    for _ in range(2):
        overnight.record_consumption(
            experiment_dir,
            scope_kind="campaign",
            scope_id=_CAMPAIGN_ID,
            consumed_block="campaign-watch",
            event_kind=overnight.HEAL_ATTEMPT_KIND,
            failed_at=str(utcnow().isoformat(timespec="seconds")),
            detail={"outcome": "respawned", "heal_class": "A"},
        )
    assert (
        overnight.sever_recurrence_count(experiment_dir, "campaign", _CAMPAIGN_ID, heal_class="A")
        == 2
    )

    routing = tax.escalate_if_recurring(
        experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID, threshold=2
    )
    assert routing is not None
    assert routing.heal_class == "C2"
    assert routing.arm == "report"
    assert routing.detail["recurrence_count"] == 2


# ── per-class morning-brief sections (§7.4) ───────────────────────────────────


def test_class_sections_split_by_class(experiment_dir: Path) -> None:
    now = str(utcnow().isoformat(timespec="seconds"))
    # A Class-B heal with an anchor + verify result.
    overnight.record_consumption(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        consumed_block="campaign-watch",
        event_kind=overnight.HEAL_ATTEMPT_KIND,
        failed_at=now,
        detail={
            "outcome": "respawned",
            "heal_class": "B",
            "anchor_ref": "env-pin",
            "verify_result": {"verified": True},
        },
    )
    # A Class-C2 finding.
    overnight.record_consumption(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        consumed_block="campaign-watch",
        event_kind="auto-advance",
        failed_at=now,
        detail={"heal_class": "C2", "cause": "stale-wheel"},
    )
    sections = tax.class_morning_sections(experiment_dir, "campaign", _CAMPAIGN_ID)
    assert len(sections["class_a_b_heals"]) == 1
    assert sections["class_a_b_heals"][0]["anchor_ref"] == "env-pin"
    assert len(sections["class_c2_findings"]) == 1


def test_morning_brief_folds_in_class_sections(experiment_dir: Path) -> None:
    tax.mint_env_pin_anchor(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        pinned_env={"HPC_SSH_ENGINE": None},
    )
    brief = overnight.overnight_morning_brief(
        experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID
    )
    assert "class_sections" in brief
    assert len(brief["class_sections"]["minted_anchors"]) == 1


def test_c2_report_actuates_nothing() -> None:
    finding = tax.report_c2_finding(cause="stale-wheel", scope_kind="run", scope_id="widget-run-1")
    assert finding["heal_class"] == "C2"
    assert finding["becomes_science"] is True
    assert "run-story" in finding["routing"]
