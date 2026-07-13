"""Overnight campaign self-heal — the item-8 ruling (notebook-audit.md, 2026-07-09).

USER RULING (supersedes the "defer the reconcile-tick-recency liveness marker to
run #12" note): an asleep human cannot give consent, so a live standing consent
must SELF-HEAL a dead campaign reconcile chain with a BOUNDED number of trusted
respawn attempts, then FAIL LOUD so the human is notified on waking.

Each guard FIRES here:

* dead-chain detection at the threshold (``campaign_chain_status``);
* a heal attempt respawns the sanctioned WATCHER + journals it;
* a no-op heal on a live chain (no duplicate spawn);
* cap exhaustion flips the consent DEAD + refuses further auto-advance;
* the morning brief LEADS with the failure + latency;
* the fail-loud push fires when the capability is declared, the gap is recorded
  when it is not;
* ZERO SSH anywhere in the heal path (the canonical ``ssh_run`` seam is armed to
  raise and is never reached).

TOY VOCABULARY ONLY: widget campaigns.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.infra.time import utcnow
from hpc_agent.ops import overnight
from hpc_agent.state import decision_journal as sdj

if TYPE_CHECKING:
    from pathlib import Path

_CAMPAIGN_ID = "widget-campaign"
_RUN_ID = "widget-iter-7"


def _iso(dt: Any) -> str:
    return str(dt.isoformat(timespec="seconds"))


@pytest.fixture
def experiment_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    exp = tmp_path / "exp"
    exp.mkdir()
    return exp


@pytest.fixture(autouse=True)
def _ban_cold_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm the canonical SSH seam to EXPLODE — the heal path must never dial."""
    import hpc_agent.infra.remote as remote

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the overnight self-heal path opened SSH — hard-rule violation")

    monkeypatch.setattr(remote, "ssh_run", _boom)


def _resolved(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "expires_at": _iso(utcnow() + timedelta(hours=8)),
        "budget_cap": 50.0,
        "walltime_cap": 3600,
        "cmd_sha": _CAMPAIGN_ID + "-identity",
        "wake": {"kind": "status-watch", "campaign_id": _CAMPAIGN_ID},
    }
    base.update(overrides)
    return base


def _seed_campaign_consent(experiment_dir: Path, resolved: dict[str, Any] | None = None) -> None:
    sdj.append_decision(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        block=overnight.OVERNIGHT_CONSENT_BLOCK,
        response="let the widget campaign self-chain overnight, cap 50 dollars",
        resolved=_resolved() if resolved is None else resolved,
    )


@pytest.fixture(autouse=True)
def _stub_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the campaign spec identity match the seeded consent's cmd_sha.

    ``self_heal_campaign`` recomputes the greenlit-spec identity to check the
    consent is still live; stub it (and the manifest read) so the identity is a
    stable match — the liveness/heal legs, not the spec-identity leg, are under
    test here.
    """
    import hpc_agent.meta.campaign.blocks as blocks_mod
    import hpc_agent.meta.campaign.manifest as manifest_mod

    monkeypatch.setattr(
        blocks_mod, "campaign_spec_identity", lambda *_a, **_k: _CAMPAIGN_ID + "-identity"
    )
    monkeypatch.setattr(manifest_mod, "read_manifest", lambda *_a, **_k: {"goal": "sweep widgets"})


def _seed_campaign_run_lease(experiment_dir: Path, *, run_id: str, pid: int) -> None:
    """Write a detached ``campaign-run`` lease + its spec (the local liveness state)."""
    from hpc_agent.state.run_record import _current_homedir

    detached = _current_homedir() / "_detached"
    detached.mkdir(parents=True, exist_ok=True)
    spec_path = detached / f"campaign-run-{run_id}.spec.json"
    spec_path.write_text(json.dumps({"campaign_id": _CAMPAIGN_ID}), encoding="utf-8")
    lease = detached / f"campaign-run-{run_id}.lease.json"
    lease.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "block": "campaign-run",
                "pid": pid,
                "argv": [
                    "py",
                    "campaign-run",
                    "--spec",
                    str(spec_path),
                    "--experiment-dir",
                    str(experiment_dir),
                ],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def spawn_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every ``launch_submit_block_detached`` call; return a fake handle."""
    calls: list[dict[str, Any]] = []

    from hpc_agent._kernel.lifecycle import detached as detached_mod

    class _FakeLaunch:
        run_id = _RUN_ID
        pid = 99991
        log_path = "log"
        argv: list[str] = []

    def _fake_launch(*, verb: str, experiment_dir: str, spec: dict[str, Any], **_k: Any) -> Any:
        calls.append({"verb": verb, "spec": spec})
        return _FakeLaunch()

    monkeypatch.setattr(detached_mod, "launch_submit_block_detached", _fake_launch)
    return calls


# ── liveness marker: dead-chain detection at the threshold ────────────────────


def test_dead_chain_detected_past_threshold(experiment_dir: Path) -> None:
    # A stale campaign-run lease (dead pid) + a cursor tick well past N×interval.
    _seed_campaign_run_lease(experiment_dir, run_id=_RUN_ID, pid=0)  # pid<=0 ⇒ not live
    from hpc_agent.meta.campaign.cursor import cursor_path

    stale = _iso(utcnow() - timedelta(seconds=4000))
    cursor_path(experiment_dir, _CAMPAIGN_ID).write_text(
        json.dumps({"cursor_schema_version": 1, "iteration": 3, "updated_at": stale}),
        encoding="utf-8",
    )
    status = overnight.campaign_chain_status(
        experiment_dir,
        campaign_id=_CAMPAIGN_ID,
        expected_tick_seconds=900.0,
        dead_after_multiple=3,  # threshold = 2700s; age ~4000s ⇒ dead
    )
    assert status.live is False
    assert status.reason == "dead-chain"
    assert status.age_seconds is not None and status.age_seconds > status.threshold_seconds


def test_recent_tick_reads_live(experiment_dir: Path) -> None:
    from hpc_agent.meta.campaign.cursor import cursor_path

    fresh = _iso(utcnow() - timedelta(seconds=100))
    cursor_path(experiment_dir, _CAMPAIGN_ID).write_text(
        json.dumps({"cursor_schema_version": 1, "iteration": 1, "updated_at": fresh}),
        encoding="utf-8",
    )
    status = overnight.campaign_chain_status(
        experiment_dir, campaign_id=_CAMPAIGN_ID, expected_tick_seconds=900.0, dead_after_multiple=3
    )
    assert status.live is True
    assert status.reason == "recent-tick"


def test_live_detached_worker_reads_live_despite_stale_cursor(experiment_dir: Path) -> None:
    """A long single iteration bumps no cursor — a live worker must count as live."""
    _seed_campaign_run_lease(experiment_dir, run_id=_RUN_ID, pid=os.getpid())  # alive
    from hpc_agent.meta.campaign.cursor import cursor_path

    cursor_path(experiment_dir, _CAMPAIGN_ID).write_text(
        json.dumps(
            {
                "cursor_schema_version": 1,
                "iteration": 1,
                "updated_at": _iso(utcnow() - timedelta(hours=5)),
            }
        ),
        encoding="utf-8",
    )
    status = overnight.campaign_chain_status(
        experiment_dir, campaign_id=_CAMPAIGN_ID, expected_tick_seconds=900.0, dead_after_multiple=3
    )
    assert status.live is True
    assert status.reason == "live-worker"


# ── bounded self-heal: respawn + journal ──────────────────────────────────────


def test_heal_respawns_watcher_and_journals(
    experiment_dir: Path, spawn_recorder: list[dict[str, Any]]
) -> None:
    _seed_campaign_consent(experiment_dir)
    _seed_campaign_run_lease(experiment_dir, run_id=_RUN_ID, pid=0)  # dead worker
    from hpc_agent.meta.campaign.cursor import cursor_path

    cursor_path(experiment_dir, _CAMPAIGN_ID).write_text(
        json.dumps(
            {
                "cursor_schema_version": 1,
                "iteration": 2,
                "updated_at": _iso(utcnow() - timedelta(hours=2)),
            }
        ),
        encoding="utf-8",
    )
    outcome = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    assert outcome.status == "respawned"
    # Re-armed the sanctioned WATCHER (status-watch), never campaign-run/scheduler.
    assert len(spawn_recorder) == 1
    assert spawn_recorder[0]["verb"] == "status-watch"
    assert spawn_recorder[0]["spec"]["monitor"]["run_id"] == _RUN_ID
    # The attempt was journaled (an unrecorded heal is the laundering class).
    ledger = overnight.read_consumption_ledger(experiment_dir, "campaign", _CAMPAIGN_ID)
    attempts = [line_ for line_ in ledger if line_["event_kind"] == overnight.HEAL_ATTEMPT_KIND]
    assert len(attempts) == 1
    assert attempts[0]["detail"]["outcome"] == "respawned"
    assert attempts[0]["detail"]["run_id"] == _RUN_ID


def test_heal_noop_on_live_chain_never_spawns(
    experiment_dir: Path, spawn_recorder: list[dict[str, Any]]
) -> None:
    _seed_campaign_consent(experiment_dir)
    _seed_campaign_run_lease(experiment_dir, run_id=_RUN_ID, pid=os.getpid())  # live worker
    outcome = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    assert outcome.status == "chain-live-noop"
    assert spawn_recorder == []  # no duplicate spawn
    ledger = overnight.read_consumption_ledger(experiment_dir, "campaign", _CAMPAIGN_ID)
    assert [ln for ln in ledger if ln["event_kind"] == overnight.HEAL_ATTEMPT_KIND] == []


def test_heal_dedups_when_lease_held(experiment_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A respawn against an actually-live watcher is a disclosed no-op, not a dup."""
    _seed_campaign_consent(experiment_dir)
    _seed_campaign_run_lease(experiment_dir, run_id=_RUN_ID, pid=0)  # stale campaign-run
    from hpc_agent.meta.campaign.cursor import cursor_path

    cursor_path(experiment_dir, _CAMPAIGN_ID).write_text(
        json.dumps(
            {
                "cursor_schema_version": 1,
                "iteration": 2,
                "updated_at": _iso(utcnow() - timedelta(hours=2)),
            }
        ),
        encoding="utf-8",
    )
    from hpc_agent._kernel.lifecycle import detached as detached_mod

    def _held(**_k: Any) -> Any:
        raise detached_mod.DetachedLeaseHeld("a live status-watch already owns the lease")

    monkeypatch.setattr(detached_mod, "launch_submit_block_detached", _held)
    outcome = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    assert outcome.status == "chain-live-noop"
    ledger = overnight.read_consumption_ledger(experiment_dir, "campaign", _CAMPAIGN_ID)
    attempts = [ln for ln in ledger if ln["event_kind"] == overnight.HEAL_ATTEMPT_KIND]
    assert len(attempts) == 1 and attempts[0]["detail"]["outcome"] == "noop-lease-held"
    # A lease-held no-op does NOT spend an attempt against the cap.
    assert overnight._heal_respawn_count(experiment_dir, _CAMPAIGN_ID) == 0


# ── fail loud on exhaustion: flip dead + refuse auto-advance ──────────────────


def _seed_dead_chain(experiment_dir: Path) -> None:
    _seed_campaign_run_lease(experiment_dir, run_id=_RUN_ID, pid=0)
    from hpc_agent.meta.campaign.cursor import cursor_path

    cursor_path(experiment_dir, _CAMPAIGN_ID).write_text(
        json.dumps(
            {
                "cursor_schema_version": 1,
                "iteration": 2,
                "updated_at": _iso(utcnow() - timedelta(hours=3)),
            }
        ),
        encoding="utf-8",
    )


def test_cap_exhaustion_flips_consent_dead_and_refuses(
    experiment_dir: Path, spawn_recorder: list[dict[str, Any]]
) -> None:
    _seed_campaign_consent(experiment_dir, resolved=_resolved(heal_attempts_cap=2))
    _seed_dead_chain(experiment_dir)
    # Two respawns exhaust the cap; the third consultation flips dead + fails loud.
    first = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    second = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    third = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    assert first.status == "respawned"
    assert second.status == "respawned"
    assert third.status == "exhausted"
    assert overnight.consent_marked_dead(experiment_dir, "campaign", _CAMPAIGN_ID) is True
    # The consent now REFUSES every further auto-advance.
    decision = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        current_cmd_sha=_CAMPAIGN_ID + "-identity",
    )
    assert decision.live is False
    assert decision.reason == "heal-exhausted"
    # And the boundary consumption parks with that reason.
    consume = overnight.consume_boundary_under_consent(
        experiment_dir,
        scope_kind="campaign",
        scope_id=_CAMPAIGN_ID,
        boundary_block="campaign-watch",
        current_cmd_sha=_CAMPAIGN_ID + "-identity",
    )
    assert consume.consumed is False
    assert consume.decision.reason == "heal-exhausted"
    # A further heal is an idempotent no-op (already dead).
    assert (
        overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID).status
        == "already-dead"
    )


def test_spawn_failed_counts_toward_cap(
    experiment_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deterministically-failing watcher spawn is a SPENT attempt (bug-sweep #46).

    Otherwise a persistent OSError/DriveModeError retries forever, the fail-loud
    DEAD flip never fires, and the attempts are invisible in the morning brief.
    """
    _seed_campaign_consent(experiment_dir)
    _seed_dead_chain(experiment_dir)
    from hpc_agent._kernel.lifecycle import detached as detached_mod

    def _boom(**_k: Any) -> Any:
        raise OSError("persistent spawn failure (EMFILE)")

    monkeypatch.setattr(detached_mod, "launch_submit_block_detached", _boom)
    outcome = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    assert outcome.status == "spawn-failed"
    ledger = overnight.read_consumption_ledger(experiment_dir, "campaign", _CAMPAIGN_ID)
    attempts = [ln for ln in ledger if ln["event_kind"] == overnight.HEAL_ATTEMPT_KIND]
    assert len(attempts) == 1 and attempts[0]["detail"]["outcome"] == "spawn-failed"
    # The spawn-failure DID spend an attempt against the cap.
    assert overnight._heal_respawn_count(experiment_dir, _CAMPAIGN_ID) == 1


def test_cap_consecutive_spawn_failures_flip_dead_and_fail_loud(
    experiment_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cap` consecutive spawn failures exhaust the budget and fail loud, with the
    spawn-failed attempts disclosed under the fail-loud line (bug-sweep #46)."""
    _seed_campaign_consent(experiment_dir, resolved=_resolved(heal_attempts_cap=2))
    _seed_dead_chain(experiment_dir)
    from hpc_agent._kernel.lifecycle import detached as detached_mod

    def _boom(**_k: Any) -> Any:
        raise OSError("persistent spawn failure (ENOSPC)")

    monkeypatch.setattr(detached_mod, "launch_submit_block_detached", _boom)
    first = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    second = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    third = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    assert first.status == "spawn-failed"
    assert second.status == "spawn-failed"
    assert third.status == "exhausted"
    assert overnight.consent_marked_dead(experiment_dir, "campaign", _CAMPAIGN_ID) is True
    # The morning brief now leads with a heal_failure attaching the spawn-failed
    # attempts (a HEAL_FAILED line now exists once the cap fires).
    brief = overnight.overnight_morning_brief(
        experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID
    )
    heal_failure = brief.get("heal_failure")
    assert heal_failure is not None, brief
    detail = heal_failure.get("attempts_detail") or []
    assert any(d.get("outcome") == "spawn-failed" for d in detail), heal_failure


def test_structurally_impossible_when_no_inflight_run_fails_loud(
    experiment_dir: Path, spawn_recorder: list[dict[str, Any]]
) -> None:
    """A dead chain with no iteration worker to watch fails loud immediately."""
    _seed_campaign_consent(experiment_dir)
    from hpc_agent.meta.campaign.cursor import cursor_path

    cursor_path(experiment_dir, _CAMPAIGN_ID).write_text(
        json.dumps(
            {
                "cursor_schema_version": 1,
                "iteration": 1,
                "updated_at": _iso(utcnow() - timedelta(hours=3)),
            }
        ),
        encoding="utf-8",
    )
    outcome = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    assert outcome.status == "structurally-impossible"
    assert spawn_recorder == []
    assert overnight.consent_marked_dead(experiment_dir, "campaign", _CAMPAIGN_ID) is True


# ── the morning brief leads with the failure + latency ────────────────────────


def test_morning_brief_leads_with_heal_failure_and_latency(
    experiment_dir: Path, spawn_recorder: list[dict[str, Any]]
) -> None:
    _seed_campaign_consent(experiment_dir, resolved=_resolved(heal_attempts_cap=1))
    _seed_dead_chain(experiment_dir)
    failed_at = _iso(utcnow() - timedelta(hours=2))
    overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)  # respawn (1)
    overnight.self_heal_campaign(
        experiment_dir, campaign_id=_CAMPAIGN_ID, now_iso=failed_at
    )  # dead
    brief = overnight.overnight_morning_brief(
        experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID
    )
    heal = brief["heal_failure"]
    assert heal is not None
    assert heal["failed_at"] == failed_at
    assert heal["latency_seconds"] is not None and heal["latency_seconds"] > 0
    assert heal["attempts"] == 1
    # The respawn attempts are itemized under the failure.
    assert any(a["outcome"] == "respawned" for a in heal["attempts_detail"])
    # heal_failure LEADS the brief (first key of the dict).
    assert next(iter(brief)) == "heal_failure"
    # The failure survives even if the consent were gone — morning_brief_if_any surfaces it.
    assert (
        overnight.morning_brief_if_any(experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID)
        is not None
    )


# ── push fired when capability declared, gap recorded when not ────────────────


def test_push_fired_when_capability_declared(
    experiment_dir: Path, spawn_recorder: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_campaign_consent(experiment_dir, resolved=_resolved(heal_attempts_cap=1))
    _seed_dead_chain(experiment_dir)
    monkeypatch.setattr(
        overnight,
        "notification_plan",
        lambda *_a, **_k: {
            "push_available": True,
            "channel": "watchdog alert-delivery hook",
            "gap": None,
        },
    )
    fired: list[str] = []
    import hpc_agent.ops.recover.notify as notify_mod

    def _fake_alert(text: str, **_k: Any) -> dict[str, Any]:
        fired.append(text)
        return {"mechanism": "logfile", "delivered": True}

    monkeypatch.setattr(notify_mod, "raise_alert_notification", _fake_alert)
    overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)  # respawn
    outcome = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)  # exhausted
    assert outcome.status == "exhausted"
    assert len(fired) == 1  # the push actually fired
    assert outcome.notification is not None and outcome.notification["fired"] is True


def test_gap_recorded_when_no_push_hook(
    experiment_dir: Path, spawn_recorder: list[dict[str, Any]]
) -> None:
    # Default test env has no watchdog alert-delivery hook → notification_plan gap.
    _seed_campaign_consent(experiment_dir, resolved=_resolved(heal_attempts_cap=1))
    _seed_dead_chain(experiment_dir)
    overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)  # respawn
    outcome = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)  # exhausted
    assert outcome.status == "exhausted"
    assert outcome.notification is not None
    assert outcome.notification["push_available"] is False
    brief = overnight.overnight_morning_brief(
        experiment_dir, scope_kind="campaign", scope_id=_CAMPAIGN_ID
    )
    assert brief["heal_failure"]["push_available"] is False
    assert brief["heal_failure"]["disclosure_gap"]


# ── the seat: doctor self_heal opt-in ─────────────────────────────────────────


def test_doctor_self_heal_opt_in_drives_the_scan(
    experiment_dir: Path, spawn_recorder: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from hpc_agent._wire.queries.doctor import DoctorSpec
    from hpc_agent.ops.recover.doctor import doctor

    _seed_campaign_consent(experiment_dir)
    _seed_dead_chain(experiment_dir)
    # campaign_list enumerates by sidecar tag; stub it to name our campaign.
    import hpc_agent.meta.campaign.atoms.list_campaigns as lc

    monkeypatch.setattr(
        lc,
        "campaign_list",
        lambda *, experiment_dir: {"campaigns": [{"campaign_id": _CAMPAIGN_ID, "iterations": 1}]},
    )
    # Default (no self_heal): detection only — no spawn.
    doctor(experiment_dir=experiment_dir, spec=DoctorSpec())
    assert spawn_recorder == []
    # Opt-in: the dead chain is self-healed (watcher respawned).
    doctor(experiment_dir=experiment_dir, spec=DoctorSpec(self_heal=True))
    assert len(spawn_recorder) == 1 and spawn_recorder[0]["verb"] == "status-watch"


def test_no_consent_is_a_noop(experiment_dir: Path, spawn_recorder: list[dict[str, Any]]) -> None:
    _seed_dead_chain(experiment_dir)  # dead chain but NO standing consent
    outcome = overnight.self_heal_campaign(experiment_dir, campaign_id=_CAMPAIGN_ID)
    assert outcome.status == "no-consent"
    assert spawn_recorder == []
