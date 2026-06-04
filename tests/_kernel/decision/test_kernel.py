"""Tests for the primordial decision kernel.

Pins the control flow every router shares: first matching rule resolves
the point (``decided_by="code"``); if every rule abstains, the point
escalates (``decided_by="judgement"``) carrying the abstain handler's
:class:`Escalation`. ``tally`` is the code-vs-judgement promotion signal.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hpc_agent._kernel.decision import Decision, decide, tally
from hpc_agent._wire.fixtures.escalation import CandidateAction, Escalation


@dataclass
class _Evidence:
    value: int


def _escalate(_: _Evidence) -> Escalation:
    return Escalation(
        decided_by="judgement",
        reason="no rule fired",
        candidate_actions=[CandidateAction(action="ask-human")],
    )


def test_first_matching_rule_resolves_to_code() -> None:
    decision = decide(
        "demo",
        _Evidence(value=5),
        rules=[
            lambda e: None,
            lambda e: CandidateAction(action="big", rationale="value>3") if e.value > 3 else None,
            lambda e: CandidateAction(action="never"),
        ],
        on_abstain=_escalate,
    )
    assert decision.decided_by == "code"
    assert decision.resolved is True
    assert decision.chosen is not None and decision.chosen.action == "big"
    assert decision.escalation is None
    # reason defaults to the chosen candidate's own rationale.
    assert decision.reason == "value>3"


def test_first_match_wins_short_circuits_later_rules() -> None:
    decision = decide(
        "demo",
        _Evidence(value=5),
        rules=[
            lambda e: CandidateAction(action="first"),
            lambda e: CandidateAction(action="second"),
        ],
        on_abstain=_escalate,
    )
    assert decision.chosen is not None and decision.chosen.action == "first"


def test_all_rules_abstain_escalates_to_judgement() -> None:
    decision = decide(
        "demo",
        _Evidence(value=1),
        rules=[lambda e: None, lambda e: None],
        on_abstain=_escalate,
    )
    assert decision.decided_by == "judgement"
    assert decision.resolved is False
    assert decision.chosen is None
    assert decision.escalation is not None
    assert decision.escalation.candidate_actions[0].action == "ask-human"
    # judgement reason mirrors the escalation's.
    assert decision.reason == "no rule fired"


def test_no_rules_at_all_escalates() -> None:
    decision = decide("demo", _Evidence(value=1), rules=[], on_abstain=_escalate)
    assert decision.decided_by == "judgement"


def test_reason_for_overrides_candidate_rationale() -> None:
    decision = decide(
        "demo",
        _Evidence(value=9),
        rules=[lambda e: CandidateAction(action="x", rationale="intrinsic")],
        on_abstain=_escalate,
        reason_for=lambda hit: f"chose {hit.action} deliberately",
    )
    assert decision.reason == "chose x deliberately"


def test_default_branch_keeps_it_code_for_a_total_ladder() -> None:
    # A precedence ladder whose catch-all always applies never escalates.
    decision = decide(
        "ladder",
        _Evidence(value=0),
        rules=[lambda e: CandidateAction(action="stop") if e.value > 0 else None],
        default=CandidateAction(action="continue", rationale="nothing fired"),
    )
    assert decision.decided_by == "code"
    assert decision.chosen is not None and decision.chosen.action == "continue"
    assert decision.escalation is None
    assert decision.reason == "nothing fired"


def test_rules_win_over_default() -> None:
    decision = decide(
        "ladder",
        _Evidence(value=5),
        rules=[lambda e: CandidateAction(action="stop") if e.value > 0 else None],
        default=CandidateAction(action="continue"),
    )
    assert decision.chosen is not None and decision.chosen.action == "stop"


def test_abstain_with_no_fallback_is_a_misuse() -> None:
    with pytest.raises(ValueError, match="no fallback configured|neither a default"):
        decide("demo", _Evidence(value=1), rules=[lambda e: None])


def test_tally_counts_code_vs_judgement() -> None:
    decisions = [
        Decision(point="p", decided_by="code"),
        Decision(point="p", decided_by="judgement"),
        Decision(point="p", decided_by="code"),
    ]
    assert tally(decisions) == {"code": 2, "judgement": 1}
