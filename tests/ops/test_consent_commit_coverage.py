"""Behavior-pins for the CONSENT-COMMIT path — how a human ``y`` becomes a
durable journal decision (``ops/decision/journal`` + ``state/decision_journal``).

This battery pins the CONTRACTS the repo doctrine names, that the existing
per-gate suites (``test_decision_journal_primitives``, ``test_overnight_consent``,
``test_conclusion_authorship``, ``test_append_decision_request_id``,
``test_consent_hint_coverage``) do NOT pin head-on:

* **A bare ``y`` is ALWAYS accepted** — nothing refuses a bare ``y`` at an
  ordinary boundary; the doctrine floor. Pinned as a durable-commit contract
  (what lands in the journal FILE), with the empty-response boundary on the
  other side.
* **Consent hints are DISPLAY-ONLY, never load-bearing** — the commit path never
  consults the code-composed approve-hint; hint absence or a malformed hint echo
  can never block, redirect, or leak into a commit.
* **Consequence-ranked durability / attribution / idempotency** — a consent must
  never be silently dropped, mis-attributed, or double-committed. Pinned on the
  ON-DISK bytes: the exact durable field set, the single-actor byte-identity
  (no ``attestor_id``/``request_id`` keys), the per-journal-file replay dedup,
  and the honest non-idempotency of a re-appended greenlight.
* **Overnight consent — the highest-blast consent path** — the journal-seat
  compose guard (``_compose_overnight_consent``) both directions, and a composed
  default landing durably with its disclosure.
* **Conclusion revoke-floor boundaries** — the four raise sites of
  ``_assert_conclusion_revoke_floor`` (un-pinned by the happy-path suite).

Every assertion is mutation-killing (exact values, both-sided boundaries,
polarity, identity) and its docstring/comment names the mutant it kills. Toy
vocabulary only. ``src`` is read-only for this battery — test file is add-only.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._kernel.lifecycle.consent_hint import compose_approve_hint
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.infra.time import utcnow
from hpc_agent.ops import overnight
from hpc_agent.ops.decision.journal import (
    _compose_overnight_consent,
    append_decision,
)
from hpc_agent.state.decision_journal import decisions_path, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

# ``submit-s4`` is a chain-FINAL block: the next_block default derives nothing
# (idx+1 out of range), so ``resolved`` stays exactly ``{}`` — a clean substrate
# for pinning the durable on-disk field set with no injected routing token.
_FINAL_BLOCK = "submit-s4"
_RUN = ("run", "widget-run-1")


def _append(
    experiment_dir: Path,
    *,
    scope_kind: str = "run",
    scope_id: str = "widget-run-1",
    block: str = _FINAL_BLOCK,
    response: str = "y",
    resolved: dict[str, Any] | None = None,
    proposal: Any = None,
    provenance: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "block": block,
        "response": response,
    }
    if resolved is not None:
        payload["resolved"] = resolved
    if proposal is not None:
        payload["proposal"] = proposal
    if provenance is not None:
        payload["provenance"] = provenance
    return append_decision(
        experiment_dir=experiment_dir, spec=AppendDecisionInput.model_validate(payload)
    )


def _disk_records(experiment_dir: Path, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Parse the journal FILE directly — durability read (never via the primitive)."""
    path = decisions_path(experiment_dir, scope_kind, scope_id)
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ── A. a bare ``y`` is ALWAYS accepted (durable-commit floor) ─────────────────


def test_bare_y_commits_durably(tmp_path: Path) -> None:
    """Doctrine floor: a bare ``y`` greenlight COMMITS and the record lands on
    disk with ``response == 'y'`` exactly.

    Kills: any mutant that refuses / drops a bare ``y`` at an ordinary boundary,
    or writes a response other than the one supplied.
    """
    out = _append(tmp_path, response="y", resolved={})
    assert out.count == 1
    records = _disk_records(tmp_path, *_RUN)
    assert len(records) == 1  # exactly one durable line
    assert records[0]["response"] == "y"  # the bare ack, verbatim, on disk


def test_empty_response_refused_at_both_boundaries(tmp_path: Path) -> None:
    """The other side of the floor: an EMPTY response is refused — proving the
    bare-``y`` acceptance is a real acceptance, not a blanket pass of everything.
    Refused at BOTH layers (defense in depth): the wire model's ``min_length``
    and, when the wire is bypassed, the state-layer ``if not response`` guard.

    Kills: dropping either guard (polarity) — an empty greenlight must never
    reach the journal.
    """
    from pydantic import ValidationError

    # Layer 1 — the wire boundary rejects an empty response before commit.
    with pytest.raises(ValidationError, match="at least 1 character"):
        _append(tmp_path, response="")

    # Layer 2 — bypass the wire (model_copy does not re-validate) to prove the
    # state-layer guard fires too, raising the domain SpecInvalid.
    spec = AppendDecisionInput.model_validate(
        {"scope_kind": "run", "scope_id": "widget-run-1", "block": _FINAL_BLOCK, "response": "y"}
    ).model_copy(update={"response": ""})
    with pytest.raises(errors.SpecInvalid, match="response must be a non-empty string"):
        append_decision(experiment_dir=tmp_path, spec=spec)

    # Nothing was journaled by either refused exchange.
    assert read_decisions(tmp_path, *_RUN) == []


def test_bare_y_commits_despite_a_proposal_when_no_required_caller_field(tmp_path: Path) -> None:
    """A bare ``y`` against an agent PROPOSAL still commits when ``resolved``
    introduces no REQUIRED_CALLER field — the laundering gate keys on the field,
    not on the mere presence of a proposal.

    Kills: a mutant that widened the human-authorship refusal to fire on any
    bare ``y`` that carries a proposal (which would break every ordinary
    greenlight).
    """
    out = _append(
        tmp_path,
        response="y",
        proposal="I recommend cluster=hoffman2, walltime=600s",
        resolved={"cluster": "hoffman2", "walltime_sec": 600},
    )
    assert out.count == 1
    assert out.record.resolved["cluster"] == "hoffman2"


def test_bare_y_commits_at_campaign_greenlight(tmp_path: Path) -> None:
    """The byte-identity-floor boundary (a campaign spec greenlight) accepts a
    bare ``y`` — the standing-consent bar lives on ``overnight-consent``, NOT on
    the ordinary campaign greenlight.

    Kills: mis-routing the standing-consent authorship bar onto the plain
    campaign-greenlight block.
    """
    out = _append(
        tmp_path,
        scope_kind="campaign",
        scope_id="widget-camp",
        block="campaign-greenlight",
        response="y",
        resolved={},
    )
    assert out.count == 1
    assert _disk_records(tmp_path, "campaign", "widget-camp")[0]["response"] == "y"


# ── B. consent hints are DISPLAY-ONLY, never load-bearing ─────────────────────


def test_commit_proceeds_where_no_hint_is_composable(tmp_path: Path) -> None:
    """Hint ABSENCE never blocks the commit: at a boundary where the composer
    yields no scoped hint (no ``run_id`` to name), a bare ``y`` still commits.

    Kills: coupling the commit to the presence of a composed approve-hint.
    """
    # The composer declines (nothing scoped to name) ...
    assert compose_approve_hint(workflow="submit", successor="submit-s4", run_id=None) is None
    # ... and the commit proceeds regardless.
    out = _append(tmp_path, response="y", resolved={})
    assert out.count == 1


def test_malformed_hint_echo_never_redirects_or_leaks_into_the_commit(tmp_path: Path) -> None:
    """A malformed hint echoed into ``provenance`` is INERT — the commit path
    never reads it. The block's OWN chain successor is journaled (``submit-s2``),
    never the hint's bogus ``next_block``; the hint's stray ``cluster`` never
    leaks into ``resolved``.

    Kills: any mutant that let the commit path consult a caller-echoed
    approve-hint / its ``scope_tokens`` (a load-bearing hint would be a silent
    scope-forgery channel).
    """
    out = _append(
        tmp_path,
        block="submit-s1",  # chain successor is submit-s2
        response="y",
        resolved={},
        provenance={
            "approve_hint": {"scope_tokens": {"next_block": "WRONG-BLOCK", "cluster": "ghost"}}
        },
    )
    assert out.count == 1
    # The machine-computed successor wins; the hint's forged next_block is ignored.
    assert out.record.resolved["next_block"] == "submit-s2"
    assert out.record.resolved.get("next_block") != "WRONG-BLOCK"
    # The hint's stray cluster never entered the committed resolved.
    assert "cluster" not in out.record.resolved
    # The provenance is stored inertly, exactly as given (not interpreted).
    assert (
        _disk_records(tmp_path, *_RUN)[0]["provenance"]["approve_hint"]["scope_tokens"][
            "next_block"
        ]
        == "WRONG-BLOCK"
    )


def test_hint_composer_declares_bare_ok_and_the_commit_honors_it(tmp_path: Path) -> None:
    """The DISPLAY contract (``bare_ok=True``) and the COMMIT contract agree:
    the composer advertises that a bare ``y`` is accepted, and the commit path
    accepts it.

    Kills: a drift where the hint claims ``bare_ok`` but the commit refuses a
    bare ``y`` (or vice versa).
    """
    hint = compose_approve_hint(workflow="submit", successor="submit-s2", run_id="widget-run-1")
    assert hint is not None
    assert hint["bare_ok"] is True
    out = _append(tmp_path, block="submit-s1", response="y", resolved={})
    assert out.count == 1


# ── C. durability / attribution / idempotency (on-disk, consequence-ranked) ───


def test_durable_field_set_and_single_actor_byte_identity(tmp_path: Path) -> None:
    """The exact durable record shape for a bare-``y`` greenlight, read from the
    FILE — nothing dropped, nothing mis-attributed.

    Kills: dropping a persisted field; stamping ``attestor_id``/``request_id``
    under the single-actor no-request world (the byte-identity pin); a wrong
    ``schema_version``.
    """
    _append(tmp_path, response="y", resolved={})
    (rec,) = _disk_records(tmp_path, *_RUN)
    assert rec["schema_version"] == 1
    assert rec["scope_kind"] == "run"
    assert rec["scope_id"] == "widget-run-1"
    assert rec["block"] == _FINAL_BLOCK
    assert rec["response"] == "y"
    assert rec["resolved"] == {}
    assert rec["ts"] and rec["ts"][:4].isdigit()  # auto-stamped ISO ts present
    # Byte-identity: the additive multi-human / replay keys are ABSENT.
    assert "attestor_id" not in rec
    assert "request_id" not in rec


def test_request_id_replay_is_a_no_op_and_is_stamped_on_disk(tmp_path: Path) -> None:
    """A client-minted ``request_id`` makes a re-append a REPLAY no-op: one
    durable line, the ORIGINAL ``ts`` re-surfaced, and the id stamped on disk.

    Kills: dropping the replay-dedup ``dedup_key`` (a second line would land —
    the double-commit class); failing to stamp the ``request_id``.
    """
    first = _append(tmp_path, response="y", resolved={}, provenance={"request_id": "rpc-1"})
    replay = _append(tmp_path, response="y", resolved={}, provenance={"request_id": "rpc-1"})
    assert replay.count == 1  # no second line
    assert first.record.ts == replay.record.ts  # original re-surfaced
    (rec,) = _disk_records(tmp_path, *_RUN)
    assert rec["request_id"] == "rpc-1"  # durable attribution key


def test_replay_dedup_is_scoped_to_a_single_journal_file(tmp_path: Path) -> None:
    """The replay key is per-journal-FILE: the SAME ``request_id`` in two
    different scopes is NOT a collision — each lands its own record.

    Kills: a mutant that globalized the dedup key across journals (which would
    silently DROP a legitimate second-scope greenlight).
    """
    _append(tmp_path, scope_id="run-a", response="y", resolved={}, provenance={"request_id": "x"})
    _append(tmp_path, scope_id="run-b", response="y", resolved={}, provenance={"request_id": "x"})
    assert len(_disk_records(tmp_path, "run", "run-a")) == 1
    assert len(_disk_records(tmp_path, "run", "run-b")) == 1


def test_no_request_id_double_append_is_honestly_non_idempotent(tmp_path: Path) -> None:
    """Without a ``request_id`` the journal is an append-only audit log: the SAME
    bare-``y`` greenlight appended twice records TWO durable lines (it never
    dedups) — the honesty the primitive declares (``idempotent=False``).

    Kills: a mutant that silently deduped un-keyed appends (which would erase a
    genuine second human touchpoint from the audit trail).
    """
    _append(tmp_path, response="y", resolved={})
    second = _append(tmp_path, response="y", resolved={})
    assert second.count == 2
    records = _disk_records(tmp_path, *_RUN)
    assert len(records) == 2
    assert [r["response"] for r in records] == ["y", "y"]


# ── D. overnight consent — the highest-blast consent path (compose seat) ───────


def _overnight_spec(
    *, block: str = overnight.OVERNIGHT_CONSENT_BLOCK, scope_kind: str = "run"
) -> AppendDecisionInput:
    return AppendDecisionInput.model_validate(
        {
            "scope_kind": scope_kind,
            "scope_id": "widget-run-1",
            "block": block,
            "response": "let it run overnight to the widget canary",
            "resolved": {},
        }
    )


def test_compose_seat_passes_non_consent_block_through_untouched(tmp_path: Path) -> None:
    """The journal compose-seat guard: a NON-``overnight-consent`` block returns
    the SAME ``resolved`` object, unmodified (no composed defaults injected).

    Kills: dropping the ``spec.block != OVERNIGHT_CONSENT_BLOCK`` guard — an
    ordinary greenlight must never acquire overnight caps/wake.
    """
    resolved_in: dict[str, Any] = {"cluster": "hoffman2"}
    out = _compose_overnight_consent(tmp_path, _overnight_spec(block="submit-s2"), resolved_in)
    assert out is resolved_in  # exact identity — untouched
    assert "composed_defaults" not in resolved_in


def test_compose_seat_passes_off_scope_consent_block_through_untouched(tmp_path: Path) -> None:
    """The second seat guard: an ``overnight-consent`` block on a scope OUTSIDE
    ``{run, campaign}`` returns ``resolved`` untouched (the authorship gate then
    owns the refusal — composition never masks a bad scope).

    Kills: dropping the ``scope_kind not in CONSENT_SCOPE_KINDS`` guard.
    """
    resolved_in: dict[str, Any] = {"cmd_sha": "a3f2c9d1beef"}
    out = _compose_overnight_consent(tmp_path, _overnight_spec(scope_kind="notebook"), resolved_in)
    assert out is resolved_in  # untouched — no compose off a non-consent scope


def test_compose_seat_composes_for_a_run_scope_consent(tmp_path: Path) -> None:
    """The positive branch: an ``overnight-consent`` on a ``run`` scope IS
    composed — a fresh dict carrying the disclosed ``composed_defaults`` (never
    the input object).

    Kills: a mutant that skipped composition for a genuine run-scope consent
    (which would push the caps/wake refusals back onto the human as a NO-GO).
    """
    resolved_in: dict[str, Any] = {"cmd_sha": "a3f2c9d1beef0011"}
    out = _compose_overnight_consent(tmp_path, _overnight_spec(), resolved_in)
    assert out is not resolved_in  # a new, composed dict
    assert out is not None
    assert "composed_defaults" in out
    assert out["cmd_sha"] == "a3f2c9d1beef0011"  # the identity binding is preserved


def test_overnight_consent_composed_default_lands_durably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end on the highest-blast path: a run-scope overnight consent whose
    caps the human omitted commits with the COMPOSED default persisted AND
    disclosed in ``composed_defaults`` on disk.

    Kills: a mutant that dropped the disclosure of a composed default (a silent
    scope expansion — the human never sees the cap the code chose).
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    exp = tmp_path / "exp"
    exp.mkdir()

    # Arm the wake + seed the BOUND consent the gate requires (bound-capture).
    import os

    lease = overnight._watch_lease_path("widget-run-1")
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    from hpc_agent.state.utterances import append_utterance, utterances_path

    utterances_path(exp).parent.mkdir(parents=True, exist_ok=True)
    append_utterance(
        exp,
        "let it run overnight to the widget canary, cap 50 dollars",
        bound={
            "channel": "elicitation",
            "scope_kind": "run",
            "scope_id": "widget-run-1",
            "block": overnight.OVERNIGHT_CONSENT_BLOCK,
            "subject": {
                "heal_classes": [],
                "expires_at": str((utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")),
                "cmd_sha": "a3f2c9d1beef0011",
            },
        },
    )

    # Omit expires_at -> it must be COMPOSED and disclosed.
    resolved = {
        "budget_cap": 50.0,
        "walltime_cap": 3600,
        "cmd_sha": "a3f2c9d1beef0011",
        "wake": {"kind": "status-watch", "run_id": "widget-run-1"},
    }
    out = append_decision(
        experiment_dir=exp,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": "run",
                "scope_id": "widget-run-1",
                "block": overnight.OVERNIGHT_CONSENT_BLOCK,
                "response": "let it run overnight to the widget canary, cap 50 dollars",
                "resolved": resolved,
            }
        ),
    )
    assert out.count == 1
    (rec,) = read_decisions(exp, "run", "widget-run-1")
    assert "expires_at" in rec["resolved"]["composed_defaults"]  # composed + DISCLOSED
    composed = overnight.parse_iso_utc_or_none(rec["resolved"]["expires_at"])
    assert composed is not None and composed > utcnow()  # a live future boundary


# ── E. conclusion revoke-floor boundaries (the four raise sites) ──────────────


def _revoke(tmp_path: Path, *, response: str, resolved: dict[str, Any]) -> Any:
    return append_decision(
        experiment_dir=tmp_path,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": "conclusion",
                "scope_id": "edge-x-2025h1",
                "block": "conclusion-revoke",
                "response": response,
                "resolved": resolved,
            }
        ),
    )


def test_revoke_missing_conclusion_id_refused(tmp_path: Path) -> None:
    """Raise site 1: a revoke whose ``resolved`` names no ``conclusion_id`` is
    refused.

    Kills: dropping the ``conclusion_id`` presence check (a revoke of nothing).
    """
    with pytest.raises(errors.SpecInvalid, match="non-empty conclusion_id"):
        _revoke(tmp_path, response="withdraw it — stale", resolved={"reason": "stale data"})


def test_revoke_missing_reason_refused(tmp_path: Path) -> None:
    """Raise site 2: a revoke with a ``conclusion_id`` but NO free-text
    ``reason`` is refused (the reason is mandatory — a withdrawal is dated,
    attributed evidence).

    Kills: dropping / weakening the mandatory-``reason`` guard.
    """
    with pytest.raises(errors.SpecInvalid, match="free-text resolved\\['reason'\\]"):
        _revoke(
            tmp_path,
            response="withdraw edge-x-2025h1",
            resolved={"conclusion_id": "edge-x-2025h1"},
        )


def test_revoke_blank_reason_refused_boundary(tmp_path: Path) -> None:
    """Raise site 2, the whitespace boundary: a ``reason`` of only whitespace is
    treated as absent (``.strip()`` floor), not a real withdrawal rationale.

    Kills: dropping the ``.strip()`` — a blank-but-present reason must not pass.
    """
    with pytest.raises(errors.SpecInvalid, match="free-text resolved\\['reason'\\]"):
        _revoke(
            tmp_path,
            response="withdraw edge-x-2025h1",
            resolved={"conclusion_id": "edge-x-2025h1", "reason": "   "},
        )


def test_revoke_bare_ack_refused(tmp_path: Path) -> None:
    """Raise site 3: a bare ``y`` cannot revoke a conclusion — withdrawal is a
    HUMAN act, and the refusal carries the E2 authorship marker.

    Kills: dropping the ``_is_bare_ack`` floor on the revoke path.
    """
    with pytest.raises(errors.SpecInvalid, match="HUMAN act") as ei:
        _revoke(
            tmp_path,
            response="y",
            resolved={"conclusion_id": "edge-x-2025h1", "reason": "superseded by fresh data"},
        )
    # The E2 marker rides the refusal so the MCP popup can re-elicit.
    assert getattr(ei.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_revoke_response_not_naming_id_refused(tmp_path: Path) -> None:
    """Raise site 4: a non-bare revoke whose text does NOT name the
    ``conclusion_id`` token-exact is refused (the #26 naming floor).

    Kills: dropping the ``_names_slug`` naming leg — a withdrawal must name what
    it withdraws.
    """
    with pytest.raises(errors.SpecInvalid, match="NAME the conclusion_id"):
        _revoke(
            tmp_path,
            response="please withdraw that stale finding, it no longer holds",
            resolved={"conclusion_id": "edge-x-2025h1", "reason": "regime shift"},
        )


def test_revoke_naming_the_id_with_reason_commits(tmp_path: Path) -> None:
    """The positive side of the floor: a non-bare revoke that names the id and
    carries a reason COMMITS durably — proving the four refusals gate abuse,
    not legitimate withdrawals.

    Kills: an over-broad revoke refusal that rejects a valid withdrawal.
    """
    out = _revoke(
        tmp_path,
        response="withdraw edge-x-2025h1 — the 2025H1 window no longer holds",
        resolved={"conclusion_id": "edge-x-2025h1", "reason": "vol regime shift"},
    )
    assert out.count == 1
    (rec,) = _disk_records(tmp_path, "conclusion", "edge-x-2025h1")
    assert rec["block"] == "conclusion-revoke"
    assert rec["resolved"]["conclusion_id"] == "edge-x-2025h1"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
