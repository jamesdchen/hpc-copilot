"""T5 — the challenge authorship gate (``ops/decision/journal.py``).

Fires each lock of ``_assert_challenge_authorship`` /
``_assert_challenge_verdict_authorship`` on a synthetic violation and drives the
happy filing / verdict / withdraw round-trips end to end. The gate is the C-gate
three-lock structure (the ``_assert_conclusion_authorship`` sibling):

* Lock 1 — no affordance (organizational; pinned in the T9 contract suite).
* Lock 2 — recompute: the TARGET resolved server-side and confirmed committed at
  the asserted sha; every citation resolved against the LIVE stores; an
  unresolvable / mismatched target or citation refuses; ``content_sha`` binds
  through the ONE attestation kernel.
* Lock 3 — authorship: bare ack refused; the response NAMES the ``challenge_id``
  token-exact AND the TARGET sha by an 8+ hex prefix AND a cited sha by prefix.
  The verdict/withdraw floor mirrors it (a DISMISSAL additionally names a cited
  sha); a carried ``view_sha`` is recomputed against the challenge-status render.

The tiered evidence source (utterance-log LOCK vs journal-response FRICTION) is
the shared ``_registration_authored_text`` path, already exercised by the
registration / reproduction-verdict suites — not re-fired here.

TOY VOCABULARY ONLY (the plan's fixture rule): widget lineage, never a real
domain's words.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state.challenges import ChallengeStatus, StandingChallenges, reduce_challenge
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

# The TARGET rides a run's cmd_sha (a ``run``-kind address resolves + matches
# against the sidecar); the CITATION rides a DIFFERENT run so the two sha-prefix
# legs of Lock 3 fire independently. Both shas are >8 hex so the prefix bar has room.
_TGT_RUN = "widget-run-tgt"
_TGT_SHA = "a3f2c9d1beef0011223344556677"
_CIT_RUN = "widget-run-cite"
_CIT_SHA = "c1d2e3f4aa11bb22cc33dd44ee55"

_CHALLENGE_ID = "widget-concl-dissent"


def _seed_runs(experiment_dir: Path) -> None:
    for run_id, cmd_sha in ((_TGT_RUN, _TGT_SHA), (_CIT_RUN, _CIT_SHA)):
        write_run_sidecar(
            experiment_dir,
            run_id=run_id,
            cmd_sha=cmd_sha,
            hpc_agent_version="0.0.0-test",
            submitted_at="2025-11-14T00:00:00Z",
            executor="widget_executor.py",
            result_dir_template="results/{run_id}",
            task_count=1,
            tasks_py_sha="tasks-sha-aaa",
        )


def _target(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "run",
        "subject_kind": "conclusion",
        "subject_id": _TGT_RUN,
        "content_sha": _TGT_SHA,
        "scope": {"scope_kind": "run", "scope_id": _TGT_RUN},
    }
    base.update(overrides)
    return base


def _resolved(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "challenge_id": _CHALLENGE_ID,
        "target": _target(),
        "citations": [{"kind": "run", "ref": _CIT_RUN, "sha": _CIT_SHA}],
        "grounds": "the widget batch replication did not reproduce the concluded row",
    }
    base.update(overrides)
    return base


def _file(experiment_dir: Path, *, response: str, **resolved_overrides: Any) -> Any:
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "challenge",
            "scope_id": _CHALLENGE_ID,
            "block": "challenge",
            "response": response,
            "resolved": _resolved(**resolved_overrides),
        }
    )
    return append_decision(experiment_dir=experiment_dir, spec=spec)


# Satisfies Lock 3: names the id + the TARGET sha (a3f2c9d1) + a cited sha (c1d2e3f4).
_GOOD_FILING = (
    "challenge widget-concl-dissent — the row at a3f2c9d1 is refuted by the replication c1d2e3f4"
)


# ── happy paths ────────────────────────────────────────────────────────────────


def test_happy_filing_round_trip_binds_content_sha(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    out = _file(tmp_path, response=_GOOD_FILING)
    assert out.count == 1
    # The gate hash-locked the verified {target, citations} set into resolved.content_sha.
    assert out.record.resolved["content_sha"]
    recs = read_decisions(tmp_path, "challenge", _CHALLENGE_ID)
    status = reduce_challenge(recs, challenge_id=_CHALLENGE_ID)
    assert status.status == "open"


def test_happy_verdict_upheld_round_trip(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    _file(tmp_path, response=_GOOD_FILING)
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "challenge",
            "scope_id": _CHALLENGE_ID,
            "block": "challenge-verdict",
            "response": "uphold widget-concl-dissent — the replication stands",
            "resolved": {
                "challenge_id": _CHALLENGE_ID,
                "verdict": "upheld",
                "reasoning": "the batch row does not reproduce; the finding is refuted",
            },
        }
    )
    append_decision(experiment_dir=tmp_path, spec=spec)
    recs = read_decisions(tmp_path, "challenge", _CHALLENGE_ID)
    assert reduce_challenge(recs, challenge_id=_CHALLENGE_ID).status == "upheld"


def test_happy_dismissal_names_cited_sha(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    _file(tmp_path, response=_GOOD_FILING)
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "challenge",
            "scope_id": _CHALLENGE_ID,
            "block": "challenge-verdict",
            "response": "dismiss widget-concl-dissent — the replication c1d2e3f4 used a stale seed",
            "resolved": {
                "challenge_id": _CHALLENGE_ID,
                "verdict": "dismissed",
                "reasoning": "the cited replication c1d2e3f4 ran a stale seed; the finding holds",
            },
        }
    )
    append_decision(experiment_dir=tmp_path, spec=spec)
    recs = read_decisions(tmp_path, "challenge", _CHALLENGE_ID)
    assert reduce_challenge(recs, challenge_id=_CHALLENGE_ID).status == "dismissed"


def test_happy_withdraw_round_trip(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    _file(tmp_path, response=_GOOD_FILING)
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "challenge",
            "scope_id": _CHALLENGE_ID,
            "block": "challenge-withdraw",
            "response": "withdraw widget-concl-dissent — I no longer stand on it",
            "resolved": {
                "challenge_id": _CHALLENGE_ID,
                "reason": "a re-run reproduced the row; my dissent no longer holds",
            },
        }
    )
    append_decision(experiment_dir=tmp_path, spec=spec)
    recs = read_decisions(tmp_path, "challenge", _CHALLENGE_ID)
    assert reduce_challenge(recs, challenge_id=_CHALLENGE_ID).status == "withdrawn"


# ── Lock 2 fire tests (target existence + citation recompute) ──────────────────


def test_unresolvable_target_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="UNRESOLVABLE"):
        _file(tmp_path, response=_GOOD_FILING, target=_target(subject_id="no-such-run"))


def test_fabricated_target_sha_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="NO committed"):
        _file(tmp_path, response=_GOOD_FILING, target=_target(content_sha="deadbeefdeadbeef"))


def test_fabricated_citation_sha_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="MISMATCH"):
        _file(
            tmp_path,
            response=_GOOD_FILING,
            citations=[{"kind": "run", "ref": _CIT_RUN, "sha": "deadbeefdeadbeef"}],
        )


def test_empty_citations_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="NON-EMPTY"):
        _file(tmp_path, response=_GOOD_FILING, citations=[])


def test_empty_grounds_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="grounds"):
        _file(tmp_path, response=_GOOD_FILING, grounds="")


# ── Lock 3 fire tests (authorship) ─────────────────────────────────────────────


def test_bare_ack_filing_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="HUMAN act"):
        _file(tmp_path, response="y")


def test_filing_missing_challenge_id_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="NAME the challenge_id"):
        _file(tmp_path, response="the row at a3f2c9d1 is refuted by c1d2e3f4")


def test_filing_missing_target_sha_prefix_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    # Names the id + a cited sha, but NOT the target sha.
    with pytest.raises(errors.SpecInvalid, match="TARGET's content_sha"):
        _file(tmp_path, response="challenge widget-concl-dissent — refuted by c1d2e3f4")


def test_filing_missing_citation_sha_prefix_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    # Names the id + the target sha, but NOT a cited sha.
    with pytest.raises(errors.SpecInvalid, match="CITED sha"):
        _file(tmp_path, response="challenge widget-concl-dissent — the row at a3f2c9d1 is wrong")


# ── verdict / withdraw floor fire tests ────────────────────────────────────────


def _verdict_spec(response: str, **resolved: Any) -> AppendDecisionInput:
    base = {"challenge_id": _CHALLENGE_ID, "verdict": "dismissed", "reasoning": "r"}
    base.update(resolved)
    return AppendDecisionInput.model_validate(
        {
            "scope_kind": "challenge",
            "scope_id": _CHALLENGE_ID,
            "block": "challenge-verdict",
            "response": response,
            "resolved": base,
        }
    )


def test_bare_ack_verdict_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    _file(tmp_path, response=_GOOD_FILING)
    with pytest.raises(errors.SpecInvalid, match="HUMAN act"):
        append_decision(experiment_dir=tmp_path, spec=_verdict_spec("y"))


def test_dismissal_not_naming_cited_sha_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    _file(tmp_path, response=_GOOD_FILING)
    # Names the id but no cited sha prefix — a dismissal must engage the evidence.
    with pytest.raises(errors.SpecInvalid, match="DISMISSAL must NAME"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_verdict_spec("dismiss widget-concl-dissent, unconvinced"),
        )


def test_missing_reasoning_refused(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    _file(tmp_path, response=_GOOD_FILING)
    with pytest.raises(errors.SpecInvalid, match="reasoning"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_verdict_spec("uphold widget-concl-dissent", verdict="upheld", reasoning=""),
        )


# ── block / scope convention (both directions) ─────────────────────────────────


def test_challenge_block_refused_for_non_challenge_scope(tmp_path: Path) -> None:
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "run",
            "scope_id": _TGT_RUN,
            "block": "challenge",
            "response": "y",
        }
    )
    with pytest.raises(errors.SpecInvalid, match="challenge-family block"):
        append_decision(experiment_dir=tmp_path, spec=spec)


def test_challenge_scope_refuses_foreign_block(tmp_path: Path) -> None:
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "challenge",
            "scope_id": _CHALLENGE_ID,
            "block": "some-other-block",
            "response": "y",
        }
    )
    with pytest.raises(errors.SpecInvalid, match="accepts only its block family"):
        append_decision(experiment_dir=tmp_path, spec=spec)


# ── the C-verb view_sha recompute (reuse the op's pure render) ──────────────────
# The gate recomputes a carried view_sha via the real op path; the op routes
# through the ONE collector (``standing_challenges``), monkeypatched here to the
# SAME real ``StandingChallenges`` bundle shape the op's own T3 tests feed it.


def _op_status() -> ChallengeStatus:
    target = {
        "kind": "run",
        "subject_kind": "conclusion",
        "subject_id": _TGT_RUN,
        "content_sha": _TGT_SHA,
        "scope": {"scope_kind": "run", "scope_id": _TGT_RUN},
    }
    return ChallengeStatus(
        challenge_id=_CHALLENGE_ID,
        status="open",
        target=target,
        filing={
            "challenge_id": _CHALLENGE_ID,
            "target": target,
            "citations": [{"kind": "run", "ref": _CIT_RUN, "sha": _CIT_SHA}],
            "grounds": "the widget batch replication did not reproduce the row",
            "content_sha": _TGT_SHA,
        },
        filed_at="2026-07-01T00:00:00+00:00",
        content_sha=_TGT_SHA,
        verdict=None,
        reasoning=None,
        resolved_at=None,
        superseded=False,
    )


def _stub_op(monkeypatch: pytest.MonkeyPatch) -> Any:
    # importlib (not a static import) so this test's own type-check does not follow
    # into the op module across the subject boundary — the same reason the gate
    # reaches the op this way (the export_dossier ops-facade precedent).
    import importlib

    op = importlib.import_module("hpc_agent.ops.challenge_status_op")

    def _standing(experiment_dir: Any, **kw: Any) -> StandingChallenges:
        return StandingChallenges(
            experiment_dir=str(experiment_dir),
            statuses=(_op_status(),),
            contested=None,
            skipped=(),
        )

    monkeypatch.setattr(op, "standing_challenges", _standing)
    return op


def _correct_view_sha(op: Any, tmp_path: Path) -> str:
    result = op.challenge_status(
        experiment_dir=tmp_path,
        spec=op.ChallengeStatusSpec(challenge_id=_CHALLENGE_ID),
    )
    return str(result.view_sha)


def test_verdict_with_matching_view_sha_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_runs(tmp_path)
    _file(tmp_path, response=_GOOD_FILING)
    op = _stub_op(monkeypatch)
    view_sha = _correct_view_sha(op, tmp_path)
    append_decision(
        experiment_dir=tmp_path,
        spec=_verdict_spec(
            "uphold widget-concl-dissent",
            verdict="upheld",
            reasoning="the replication stands",
            view_sha=view_sha,
        ),
    )
    recs = read_decisions(tmp_path, "challenge", _CHALLENGE_ID)
    assert reduce_challenge(recs, challenge_id=_CHALLENGE_ID).status == "upheld"


def test_verdict_with_stale_view_sha_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_runs(tmp_path)
    _file(tmp_path, response=_GOOD_FILING)
    _stub_op(monkeypatch)
    with pytest.raises(errors.SpecInvalid, match="view_sha"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_verdict_spec(
                "uphold widget-concl-dissent",
                verdict="upheld",
                reasoning="the replication stands",
                view_sha="0" * 64,
            ),
        )
