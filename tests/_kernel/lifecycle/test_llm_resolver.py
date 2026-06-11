"""Tests for :mod:`hpc_agent._kernel.lifecycle.llm_resolver`.

The wrapper's protocol, exercised against fake inner resolvers and a
fake :class:`ChatModel` (no LLM, no network): pass-through on success,
no-LLM park on un-menued residues, the adjudicate→apply→retry round
trip, the always-honored "park" verdict, the no-progress guard, the
closed-menu post_validate (a bogus choice is repaired), and graceful
parking when the model never produces a valid adjudication.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hpc_agent._kernel.extension.spawn_prompt import WorkerDecision, WorkerReport
from hpc_agent._kernel.lifecycle.llm_resolver import (
    PARK,
    LlmJudgementResolver,
    default_apply_decision,
)

_EXIT_OK = 0
_EXIT_RESIDUE = 3

# The campaign 'path' point is decided_by=judgement in DECISION_POINTS, so
# the merged reports exercise the non-empty-why contract check too.
_MENU = {"path": ["manual", "strategy"]}


def _residue_report(point: str = "path", outcome: str = "unclassifiable") -> WorkerReport:
    return WorkerReport(
        result={"residue": True, "point": point, "outcome": outcome},
        decisions=[WorkerDecision(point=point, outcome=outcome, why="parked by inner")],
        anomalies="ESCALATION (parked, not guessed): inner could not classify.",
    )


def _ok_report() -> WorkerReport:
    return WorkerReport(result={"submitted": True}, decisions=[], anomalies="")


def _request() -> dict[str, Any]:
    return {"workflow": "campaign", "fields": {"campaign_id": "c1", "step": "decide"}}


class _FakeModel:
    """ChatModel stub returning canned completions, recording every call."""

    name = "fake"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[list[Any], dict | None]] = []

    def complete(self, messages: list[Any], *, schema: dict | None = None) -> str:
        self.calls.append((list(messages), schema))
        return self._replies.pop(0)


def _choice(chosen: str, why: str = "because evidence") -> str:
    return json.dumps({"chosen": chosen, "why": why})


def test_success_passes_through_without_llm(tmp_path: Path) -> None:
    model = _FakeModel([])

    def inner(req: dict, exp: Path) -> tuple[WorkerReport, int]:
        return _ok_report(), _EXIT_OK

    resolver = LlmJudgementResolver(inner=inner, model=model, menu=_MENU)
    report, code = resolver(_request(), tmp_path)
    assert code == _EXIT_OK
    assert report.result == {"submitted": True}
    assert model.calls == []


def test_unmenued_residue_parks_without_llm(tmp_path: Path) -> None:
    # 'decide' is a valid campaign point but absent from the menu: a genuine
    # interview-shaped park must never burn an LLM call.
    model = _FakeModel([])

    def inner(req: dict, exp: Path) -> tuple[WorkerReport, int]:
        return _residue_report(point="decide", outcome="needs_interview"), _EXIT_RESIDUE

    resolver = LlmJudgementResolver(inner=inner, model=model, menu=_MENU)
    report, code = resolver(_request(), tmp_path)
    assert code == _EXIT_RESIDUE
    assert report.result["residue"] is True
    assert model.calls == []


def test_adjudicates_applies_and_retries_to_success(tmp_path: Path) -> None:
    model = _FakeModel([_choice("strategy", "tasks.py reads prior history")])
    seen_requests: list[dict] = []

    def inner(req: dict, exp: Path) -> tuple[WorkerReport, int]:
        seen_requests.append(req)
        resolved = (req.get("fields") or {}).get("resolved") or {}
        if resolved.get("path") == "strategy":
            return _ok_report(), _EXIT_OK
        return _residue_report(), _EXIT_RESIDUE

    resolver = LlmJudgementResolver(inner=inner, model=model, menu=_MENU)
    report, code = resolver(_request(), tmp_path)

    assert code == _EXIT_OK
    assert len(model.calls) == 1
    # The decision fed back through the fields.resolved channel.
    assert seen_requests[-1]["fields"]["resolved"] == {"path": "strategy"}
    # The original request was not mutated (default_apply_decision copies).
    assert "resolved" not in (seen_requests[0].get("fields") or {})
    # The adjudication rides the final report as a judgement decision with
    # the model's rationale and the rejected alternatives.
    adjudication = report.decisions[-1]
    assert adjudication.point == "path"
    assert adjudication.chosen == "strategy"
    assert adjudication.why == "tasks.py reads prior history"
    assert set(adjudication.rejected) == {"manual", PARK}
    # The evidence prompt carried the residue and the closed menu.
    user_turn = json.loads(model.calls[0][0][-1].content)
    assert user_turn["residue"]["point"] == "path"
    assert user_turn["candidates"] == ["manual", "strategy", PARK]


def test_park_verdict_is_honored(tmp_path: Path) -> None:
    model = _FakeModel([_choice(PARK, "evidence insufficient; needs a human")])

    def inner(req: dict, exp: Path) -> tuple[WorkerReport, int]:
        return _residue_report(), _EXIT_RESIDUE

    resolver = LlmJudgementResolver(inner=inner, model=model, menu=_MENU)
    report, code = resolver(_request(), tmp_path)
    assert code == _EXIT_RESIDUE
    assert len(model.calls) == 1
    assert report.decisions[-1].chosen == PARK
    assert "evidence insufficient" in report.anomalies


def test_no_progress_guard_parks_after_one_call(tmp_path: Path) -> None:
    # The inner ignores the fields.resolved channel and re-emits the same
    # residue: the wrapper must park, not spin (and not spend a second call).
    model = _FakeModel([_choice("manual")])
    calls = 0

    def inner(req: dict, exp: Path) -> tuple[WorkerReport, int]:
        nonlocal calls
        calls += 1
        return _residue_report(), _EXIT_RESIDUE

    resolver = LlmJudgementResolver(inner=inner, model=model, menu=_MENU)
    report, code = resolver(_request(), tmp_path)
    assert code == _EXIT_RESIDUE
    assert calls == 2  # initial + one post-decision retry
    assert len(model.calls) == 1
    assert "re-emitted the same residue" in report.anomalies


def test_bogus_choice_is_repaired_by_structured_loop(tmp_path: Path) -> None:
    # First completion picks an off-menu value; structured()'s repair turn
    # feeds the membership error back and the second completion is valid.
    model = _FakeModel([_choice("bogus"), _choice("manual", "grid is explicit")])

    def inner(req: dict, exp: Path) -> tuple[WorkerReport, int]:
        resolved = (req.get("fields") or {}).get("resolved") or {}
        if resolved.get("path") == "manual":
            return _ok_report(), _EXIT_OK
        return _residue_report(), _EXIT_RESIDUE

    resolver = LlmJudgementResolver(inner=inner, model=model, menu=_MENU)
    report, code = resolver(_request(), tmp_path)
    assert code == _EXIT_OK
    assert len(model.calls) == 2
    assert report.decisions[-1].chosen == "manual"


def test_model_never_valid_parks_gracefully(tmp_path: Path) -> None:
    model = _FakeModel(["not json", "still not json", "nope"])

    def inner(req: dict, exp: Path) -> tuple[WorkerReport, int]:
        return _residue_report(), _EXIT_RESIDUE

    resolver = LlmJudgementResolver(inner=inner, model=model, menu=_MENU, max_repairs=2)
    report, code = resolver(_request(), tmp_path)
    assert code == _EXIT_RESIDUE
    assert "failed structured validation" in report.anomalies


def test_default_apply_decision_copies_not_mutates() -> None:
    original = {"workflow": "campaign", "fields": {"campaign_id": "c1"}}
    updated = default_apply_decision("path", "strategy", original)
    assert updated["fields"]["resolved"] == {"path": "strategy"}
    assert "resolved" not in original["fields"]
    assert updated["fields"]["campaign_id"] == "c1"


def test_contract_violation_is_annotated_not_raised(tmp_path: Path) -> None:
    # A third-party inner emitting a residue point not enumerated for the
    # workflow must not crash the tick loop: the resolver's contract is to
    # RETURN (report, exit_code), so the violation is annotated instead.
    model = _FakeModel([_choice("x")])

    def inner(req: dict, exp: Path) -> tuple[WorkerReport, int]:
        return _residue_report(point="bogus_point", outcome="weird"), _EXIT_RESIDUE

    resolver = LlmJudgementResolver(inner=inner, model=model, menu={"bogus_point": ["x"]})
    report, code = resolver(_request(), tmp_path)
    assert code == _EXIT_RESIDUE
    assert "CONTRACT VIOLATION" in report.anomalies
