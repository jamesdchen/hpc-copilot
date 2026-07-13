"""B4 ts>=anchor fix-wave — the shared utterance-freshness filter.

Every authorship gate whose evidence is NAMING over the harness utterance log
now bounds the pool to utterances logged AT OR AFTER the target's own timestamp:

* scope-unlock — anchor = the scope's newest LOCK record ts;
* registration-revoke / conclusion-revoke / challenge-verdict / challenge-withdraw
  — anchor = the target FILING record ts.

Without the bound, the utterance that CREATED the target (which named its id)
permanently satisfies the naming leg, so a later agent-composed revoke/verdict/
unlock rides through (the philosophy-audit B4 exposure). Each gate gets a FIRES
case (a pre-anchor utterance no longer satisfies the gate) and a PASSES case (a
post-anchor utterance still commits).

The target filing records and the utterance log are seeded DIRECTLY with
controlled timestamps — the gates read the raw journal (:func:`_read_decisions`)
and the raw utterance log, so a seeded record is exactly what they see, and the
seconds-resolution ``ts`` comparison is made deterministic (real ``now`` stamps
would collide within a test's single second).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import (
    _fresh_human_texts,
    _newest_lock_ts,
    _target_record_ts,
    append_decision,
)
from hpc_agent.state.decision_journal import decisions_path
from hpc_agent.state.run_record import journal_dir
from hpc_agent.state.utterances import utterances_path

if TYPE_CHECKING:
    from pathlib import Path

# Three fixed instants, seconds-resolution, so ``ts >= anchor`` is unambiguous.
_BEFORE = "2020-01-01T00:00:00+00:00"
_ANCHOR = "2020-06-01T00:00:00+00:00"
_AFTER = "2021-01-01T00:00:00+00:00"


def _seed_record(
    tmp_path: Path,
    *,
    scope_kind: str,
    scope_id: str,
    block: str,
    resolved: dict[str, Any],
    ts: str,
    response: str = "y",
) -> None:
    """Append one raw decision record with a CONTROLLED ts (the filing/lock)."""
    path = decisions_path(tmp_path, scope_kind, scope_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "schema_version": 1,
        "ts": ts,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "block": block,
        "response": response,
        "resolved": resolved,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _log_utterance_at(tmp_path: Path, text: str, ts: str) -> None:
    """Append one harness utterance with a CONTROLLED ts (the frozen 3-field shape)."""
    journal_dir(tmp_path)  # the namespace a real state write would have created
    path = utterances_path(tmp_path)
    rec = {"ts": ts, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _append(tmp_path: Path, **spec: object) -> object:
    return append_decision(experiment_dir=tmp_path, spec=AppendDecisionInput.model_validate(spec))


# ── the shared filter, in isolation ───────────────────────────────────────────


def test_fresh_human_texts_none_when_no_log(tmp_path: Path) -> None:
    assert _fresh_human_texts(tmp_path, actor_ids=[], anchor=None) is None


def test_fresh_human_texts_anchor_none_returns_unfiltered(tmp_path: Path) -> None:
    _log_utterance_at(tmp_path, "an early standing prompt", _BEFORE)
    assert _fresh_human_texts(tmp_path, actor_ids=[], anchor=None) == ["an early standing prompt"]


def test_fresh_human_texts_filters_pre_anchor(tmp_path: Path) -> None:
    from hpc_agent.infra.time import parse_iso_utc

    _log_utterance_at(tmp_path, "stale prompt", _BEFORE)
    _log_utterance_at(tmp_path, "fresh prompt", _AFTER)
    anchor = parse_iso_utc(_ANCHOR).timestamp()
    assert _fresh_human_texts(tmp_path, actor_ids=[], anchor=anchor) == ["fresh prompt"]


def test_fresh_human_texts_empty_when_all_stale(tmp_path: Path) -> None:
    from hpc_agent.infra.time import parse_iso_utc

    _log_utterance_at(tmp_path, "stale prompt", _BEFORE)
    anchor = parse_iso_utc(_ANCHOR).timestamp()
    # A present-but-all-stale log is [] (log exists, nothing fresh) — NOT None.
    assert _fresh_human_texts(tmp_path, actor_ids=[], anchor=anchor) == []


# ── scope-unlock (anchor = newest lock ts) ────────────────────────────────────


def _unlock(tmp_path: Path, response: str) -> object:
    return _append(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-unlock",
        response=response,
        resolved={"scope_action": "unlock"},
    )


def test_unlock_pre_lock_utterance_refused(tmp_path: Path) -> None:
    """FIRES: the only reopen-rationale utterance predates the lock it re-opens."""
    _seed_record(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-lock",
        resolved={"scope_action": "lock"},
        ts=_ANCHOR,
        response="lock the holdout",
    )
    _log_utterance_at(tmp_path, "please reopen the holdout scope for a confirmatory sweep", _BEFORE)
    with pytest.raises(errors.SpecInvalid) as ei:
        _unlock(tmp_path, "reopen the holdout scope for a confirmatory sweep")
    msg = str(ei.value)
    assert "scope-unlock authorship gate" in msg
    assert "ts>=anchor" in msg


def test_unlock_post_lock_utterance_passes(tmp_path: Path) -> None:
    """PASSES: the reopen rationale was typed AFTER the lock."""
    _seed_record(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-lock",
        resolved={"scope_action": "lock"},
        ts=_ANCHOR,
        response="lock the holdout",
    )
    _log_utterance_at(tmp_path, "please reopen the holdout scope for a confirmatory sweep", _AFTER)
    out = _unlock(tmp_path, "reopen the holdout scope for a confirmatory sweep")
    assert out.record.resolved["scope_action"] == "unlock"  # type: ignore[attr-defined]


# ── registration-revoke (anchor = registration filing ts) ─────────────────────


def _seed_registration(tmp_path: Path, reg_id: str, ts: str) -> None:
    _seed_record(
        tmp_path,
        scope_kind="registration",
        scope_id="reg-scope",
        block="registration",
        resolved={"registration_id": reg_id},
        ts=ts,
    )


def _revoke_registration(tmp_path: Path, reg_id: str, response: str) -> object:
    return _append(
        tmp_path,
        scope_kind="registration",
        scope_id="reg-scope",
        block="registration-revoke",
        response=response,
        resolved={"registration_id": reg_id, "reason": "the widget batch was recalled"},
    )


def test_registration_revoke_pre_filing_utterance_refused(tmp_path: Path) -> None:
    """FIRES: the utterance naming the registration predates the registration."""
    _seed_registration(tmp_path, "reg-widgets", _ANCHOR)
    _log_utterance_at(tmp_path, "register reg-widgets for the batch", _BEFORE)
    with pytest.raises(errors.SpecInvalid) as ei:
        _revoke_registration(tmp_path, "reg-widgets", "revoke the registration now")
    assert "must NAME the registration_id" in str(ei.value)


def test_registration_revoke_post_filing_utterance_passes(tmp_path: Path) -> None:
    """PASSES: a fresh utterance naming the registration commits the revoke."""
    _seed_registration(tmp_path, "reg-widgets", _ANCHOR)
    _log_utterance_at(tmp_path, "revoke reg-widgets — the widget batch was recalled", _AFTER)
    out = _revoke_registration(tmp_path, "reg-widgets", "revoke the registration now")
    assert out.record.block == "registration-revoke"  # type: ignore[attr-defined]


# ── conclusion-revoke (anchor = conclusion filing ts) ─────────────────────────


def _seed_conclusion(tmp_path: Path, concl_id: str, ts: str) -> None:
    _seed_record(
        tmp_path,
        scope_kind="conclusion",
        scope_id="concl-scope",
        block="conclusion",
        resolved={"conclusion_id": concl_id},
        ts=ts,
    )


def _revoke_conclusion(tmp_path: Path, concl_id: str, response: str) -> object:
    return _append(
        tmp_path,
        scope_kind="conclusion",
        scope_id="concl-scope",
        block="conclusion-revoke",
        response=response,
        resolved={"conclusion_id": concl_id, "reason": "superseded by fresh data"},
    )


def test_conclusion_revoke_pre_filing_utterance_refused(tmp_path: Path) -> None:
    """FIRES: the utterance naming the conclusion predates it."""
    _seed_conclusion(tmp_path, "edge-x-2025h1", _ANCHOR)
    _log_utterance_at(tmp_path, "conclude edge-x-2025h1 from the data", _BEFORE)
    with pytest.raises(errors.SpecInvalid) as ei:
        _revoke_conclusion(tmp_path, "edge-x-2025h1", "withdraw the finding")
    assert "must NAME the conclusion_id" in str(ei.value)


def test_conclusion_revoke_post_filing_utterance_passes(tmp_path: Path) -> None:
    """PASSES: a fresh utterance naming the conclusion commits the withdrawal."""
    _seed_conclusion(tmp_path, "edge-x-2025h1", _ANCHOR)
    _log_utterance_at(tmp_path, "withdraw edge-x-2025h1 — superseded by fresh data", _AFTER)
    out = _revoke_conclusion(tmp_path, "edge-x-2025h1", "withdraw the finding")
    assert out.record.block == "conclusion-revoke"  # type: ignore[attr-defined]


# ── challenge-verdict / challenge-withdraw (anchor = challenge filing ts) ──────


def _seed_challenge(tmp_path: Path, chal_id: str, ts: str) -> None:
    # The challenge journal thread is keyed by the challenge_id as scope_id.
    _seed_record(
        tmp_path,
        scope_kind="challenge",
        scope_id=chal_id,
        block="challenge",
        resolved={"challenge_id": chal_id},
        ts=ts,
    )


def _verdict(tmp_path: Path, chal_id: str, response: str) -> object:
    return _append(
        tmp_path,
        scope_kind="challenge",
        scope_id=chal_id,
        block="challenge-verdict",
        response=response,
        resolved={"challenge_id": chal_id, "verdict": "upheld", "reasoning": "I agree"},
    )


def _withdraw(tmp_path: Path, chal_id: str, response: str) -> object:
    return _append(
        tmp_path,
        scope_kind="challenge",
        scope_id=chal_id,
        block="challenge-withdraw",
        response=response,
        resolved={"challenge_id": chal_id, "reason": "I no longer stand on it"},
    )


def test_challenge_verdict_pre_filing_utterance_refused(tmp_path: Path) -> None:
    """FIRES: the utterance naming the challenge predates the filing."""
    _seed_challenge(tmp_path, "widget-dissent", _ANCHOR)
    _log_utterance_at(tmp_path, "file widget-dissent against the claim", _BEFORE)
    with pytest.raises(errors.SpecInvalid) as ei:
        _verdict(tmp_path, "widget-dissent", "uphold the challenge")
    assert "must" in str(ei.value) and "challenge_id" in str(ei.value)


def test_challenge_verdict_post_filing_utterance_passes(tmp_path: Path) -> None:
    """PASSES: a fresh utterance naming the challenge commits the verdict."""
    _seed_challenge(tmp_path, "widget-dissent", _ANCHOR)
    _log_utterance_at(tmp_path, "uphold widget-dissent — the objection holds", _AFTER)
    out = _verdict(tmp_path, "widget-dissent", "uphold the challenge")
    assert out.record.block == "challenge-verdict"  # type: ignore[attr-defined]


def test_challenge_withdraw_pre_filing_utterance_refused(tmp_path: Path) -> None:
    """FIRES: the challenger's naming utterance predates their own filing."""
    _seed_challenge(tmp_path, "widget-dissent", _ANCHOR)
    _log_utterance_at(tmp_path, "file widget-dissent against the claim", _BEFORE)
    with pytest.raises(errors.SpecInvalid) as ei:
        _withdraw(tmp_path, "widget-dissent", "withdraw the challenge")
    assert "must" in str(ei.value) and "challenge_id" in str(ei.value)


def test_challenge_withdraw_post_filing_utterance_passes(tmp_path: Path) -> None:
    """PASSES: a fresh utterance naming the challenge commits the withdrawal."""
    _seed_challenge(tmp_path, "widget-dissent", _ANCHOR)
    _log_utterance_at(tmp_path, "withdraw widget-dissent — I no longer stand on it", _AFTER)
    out = _withdraw(tmp_path, "widget-dissent", "withdraw the challenge")
    assert out.record.block == "challenge-withdraw"  # type: ignore[attr-defined]


# ── anchor helpers, directly ──────────────────────────────────────────────────


def test_newest_lock_ts_none_without_lock(tmp_path: Path) -> None:
    assert _newest_lock_ts(tmp_path, "holdout") is None


def test_target_record_ts_none_without_filing(tmp_path: Path) -> None:
    assert (
        _target_record_ts(
            tmp_path,
            scope_kind="registration",
            scope_id="reg-scope",
            filing_block="registration",
            id_field="registration_id",
            target_id="reg-widgets",
        )
        is None
    )
