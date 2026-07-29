"""The paste-ready ANSWER MENU composer (run-queue-placement-2026-07-28.md §8 S13).

``compose_answer_menu`` renders the lines a human PASTES at a parked boundary: ``y``
for the materialized default advance, the scope-naming form of that same advance,
and one line per STRUCTURED recommendation the brief already carries. The battery
below pins the two rules the seam exists to hold, both from the negative side as
well as the positive:

* **CODE-CARRIED DATA ONLY (§2).** A recommendation is rendered ONLY when it is a
  mapping with a non-empty string ``action``. Prose recommendations (the aggregate
  integrity issues, S1's ``safe_default`` mirror) render NOTHING — the menu must
  never author an alternative the brief did not carry.
* **NOTHING IS AUTO-ANSWERED.** The menu is disclosure: it reports ``bare_y_ok``
  per boundary, it never grants it, and the note says so.

Plus the mechanical invariants a downstream surface depends on: determinism,
de-duplication, the ``answer_line`` a phone notification pastes
(:func:`hpc_agent.ops.recover.notify.compose_park_notice`), and the
anomaly-terminator OVERRIDE label.
"""

from __future__ import annotations

from typing import Any

import pytest

from hpc_agent._kernel.lifecycle.answer_menu import (
    _as_structured,
    answer_menu_of,
    compose_answer_menu,
    recommendation_options,
)

_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"

_FAILED_REC = {"action": "classify-failed-tasks", "then": "resubmit-failed"}
_ABANDONED_REC = {"action": "reconcile-journal", "then": "confirm-abandoned-then-resubmit"}


def _menu(**over: Any) -> dict[str, Any] | None:
    kw: dict[str, Any] = {
        "brief": {},
        "block": "submit-s1",
        "run_id": "r1",
        "target": "submit-s2",
        "next_spec_sha": _SHA,
        "approve_hint": {"utterance": "y submit-s2 r1 @a1b2c3d4"},
    }
    kw.update(over)
    return compose_answer_menu(**kw)


# ── the default advance: `y` is the first line, and it says what it does ────────


def test_bare_y_leads_the_menu_and_names_the_materialized_spec() -> None:
    menu = _menu()
    assert menu is not None
    first = menu["options"][0]
    assert first["paste"] == "y"
    assert first["kind"] == "advance"
    assert first["target"] == "submit-s2"
    # The R3 pin the human is agreeing to, in the same 8 hex the approve hint uses.
    assert "advance to submit-s2" in first["means"]
    assert "sha a1b2c3d4" in first["means"]
    assert menu["bare_y_ok"] is True


def test_scoped_form_is_offered_and_becomes_the_answer_line() -> None:
    """A phone answer must name the run; the scoped utterance is the pasteable form."""
    menu = _menu()
    assert menu is not None
    scoped = menu["options"][1]
    assert scoped["paste"] == "y submit-s2 r1 @a1b2c3d4"
    assert scoped["kind"] == "advance-scoped"
    assert menu["answer_line"] == "y submit-s2 r1 @a1b2c3d4"


def test_answer_line_falls_back_to_bare_y_without_an_approve_hint() -> None:
    """A Row-14 composition refusal composes no hint — the menu still answers with `y`."""
    menu = _menu(approve_hint=None, next_spec_sha=None)
    assert menu is not None
    assert [o["paste"] for o in menu["options"]] == ["y"]
    assert menu["answer_line"] == "y"
    assert "sha" not in menu["options"][0]["means"]


def test_summary_states_what_a_bare_y_does() -> None:
    menu = _menu()
    assert menu is not None
    assert (
        menu["summary"]
        == "parked at submit-s1 for r1 — a bare 'y' advances to submit-s2 (spec sha a1b2c3d4)"
    )


# ── structured recommendations become paste-lines; prose never does ────────────


def test_top_level_structured_recommendation_becomes_a_paste_line() -> None:
    menu = _menu(brief={"recommendation": _FAILED_REC})
    assert menu is not None
    rec = menu["options"][-1]
    assert rec["paste"] == "classify-failed-tasks, then resubmit-failed"
    assert rec["kind"] == "recommendation"
    assert rec["action"] == "classify-failed-tasks"
    assert rec["then"] == "resubmit-failed"


def test_nested_mapping_recommendation_is_found() -> None:
    """``_watch_anomaly_brief`` data rides under ``brief["anomaly"]`` — one level down."""
    options = recommendation_options({"anomaly": {"recommendation": _ABANDONED_REC}})
    assert [o["paste"] for o in options] == [
        "reconcile-journal, then confirm-abandoned-then-resubmit"
    ]


def test_list_row_recommendations_are_found() -> None:
    """The snapshot brief carries one recommendation per anomaly ROW."""
    options = recommendation_options(
        {"anomalies": [{"recommendation": _FAILED_REC}, {"recommendation": _ABANDONED_REC}]}
    )
    assert [o["action"] for o in options] == ["classify-failed-tasks", "reconcile-journal"]


def test_repeated_recommendation_collapses_to_one_option() -> None:
    """Ten failed rows are ONE answer, not ten identical lines (the S13 friction)."""
    options = recommendation_options({"anomalies": [{"recommendation": _FAILED_REC}] * 10})
    assert len(options) == 1


def test_prose_recommendation_renders_nothing() -> None:
    """NEGATIVE (§2): the aggregate integrity issues carry PROSE — never a paste-line."""
    brief = {
        "issues": [
            {
                "issue": "missing_waves",
                "recommendation": "refuse the partial aggregate; investigate the missing waves",
            }
        ]
    }
    assert recommendation_options(brief) == []
    menu = _menu(brief=brief)
    assert menu is not None
    assert all(o["kind"] != "recommendation" for o in menu["options"])


def test_safe_default_mirror_recommendation_renders_nothing() -> None:
    """NEGATIVE: S1 mirrors each ambiguity's ``safe_default`` — a VALUE, not an action."""
    assert recommendation_options({"ambiguities": [{"recommendation": "hoffman2"}]}) == []
    assert recommendation_options({"ambiguities": [{"recommendation": None}]}) == []


def test_mapping_without_an_action_renders_nothing() -> None:
    # kills: dropping the non-empty-string ``action`` guard in ``_as_structured``.
    assert _as_structured({"then": "resubmit-failed"}) is None
    assert _as_structured({"action": ""}) is None
    assert _as_structured({"action": 7}) is None
    assert _as_structured("classify-failed-tasks") is None
    assert _as_structured(None) is None


def test_action_without_then_renders_the_bare_action() -> None:
    # kills: unconditionally appending the ", then " connective.
    options = recommendation_options({"recommendation": {"action": "reconcile-journal"}})
    assert options[0]["paste"] == "reconcile-journal"
    assert "then" not in options[0]


def test_non_dict_brief_is_empty() -> None:
    assert recommendation_options(None) == []
    assert recommendation_options("nope") == []  # type: ignore[arg-type]


def test_top_level_recommendation_is_not_double_counted() -> None:
    """kills: dropping the ``key == "recommendation"`` skip in the nested scan."""
    options = recommendation_options({"recommendation": _FAILED_REC})
    assert len(options) == 1


# ── boundaries with no materialized default ────────────────────────────────────


def test_no_target_and_no_recommendation_yields_no_menu() -> None:
    """An empty menu is never rendered — the caller omits the key entirely."""
    assert _menu(target=None, brief={}) is None


def test_no_target_still_offers_the_structured_recommendations() -> None:
    """A terminal/anomaly park with no advance still gets its alternatives listed."""
    menu = _menu(target=None, brief={"recommendation": _FAILED_REC})
    assert menu is not None
    assert menu["bare_y_ok"] is False
    assert menu["answer_line"] is None
    assert [o["kind"] for o in menu["options"]] == ["recommendation"]
    assert "no materialized default" in menu["summary"]
    assert "has nothing to advance to" in menu["lines"][-1]


def test_empty_string_target_is_not_a_default() -> None:
    # kills: dropping the ``and target`` truthiness guard.
    assert _menu(target="", brief={}) is None


# ── the anomaly-terminator OVERRIDE label ──────────────────────────────────────


def test_anomaly_terminator_labels_the_advance_an_override() -> None:
    """``block_chain.ANOMALY_TERMINATORS`` says recovery is a human branch there; the
    shared boundary rule still consumes a bare ``y``. Both facts are disclosed."""
    menu = _menu(is_anomaly_terminator=True, brief={"recommendation": _FAILED_REC})
    assert menu is not None
    assert menu["options"][0]["override"] is True
    assert "an OVERRIDE: this is an anomaly terminator" in menu["options"][0]["means"]
    assert menu["options"][1]["override"] is True
    # Still accepted — the label discloses, it does not withdraw the default.
    assert menu["bare_y_ok"] is True


def test_non_anomaly_terminator_carries_no_override_label() -> None:
    # kills: the ``is_anomaly_terminator`` guard flipped / dropped.
    menu = _menu()
    assert menu is not None
    assert menu["options"][0]["override"] is False
    assert "OVERRIDE" not in menu["options"][0]["means"]


# ── rendering + disclosure invariants ──────────────────────────────────────────


def test_text_ends_with_the_bare_y_acceptance_line() -> None:
    menu = _menu()
    assert menu is not None
    assert menu["text"] == "\n".join(menu["lines"])
    assert menu["text"].startswith("Answer menu — paste ONE line into chat")
    assert menu["lines"][-1] == "A bare 'y' is accepted at this boundary; anything else is a nudge."


def test_every_option_appears_verbatim_in_the_rendered_text() -> None:
    """A truncated paste-line would be un-pasteable — the column pads, never cuts."""
    menu = _menu(brief={"recommendation": {"action": "x" * 80, "then": "y" * 80}})
    assert menu is not None
    for option in menu["options"]:
        assert option["paste"] in menu["text"]


def test_note_pins_the_display_only_invariants() -> None:
    # kills: mutating the note away from the two load-bearing guarantees.
    menu = _menu()
    assert menu is not None
    note = menu["note"]
    assert "DISPLAY only" in note
    assert "nothing is auto-filled" in note
    assert "nothing is summarized" in note


def test_menu_is_deterministic() -> None:
    brief = {"anomalies": [{"recommendation": _FAILED_REC}, {"recommendation": _ABANDONED_REC}]}
    assert _menu(brief=brief) == _menu(brief=brief)


# ── answer_menu_of: the fail-open reader every consumer shares ──────────────────


def test_answer_menu_of_round_trips_a_composed_menu() -> None:
    menu = _menu()
    assert answer_menu_of({"answer_menu": menu}) == menu


def test_answer_menu_of_is_fail_open() -> None:
    """A brief from a driver predating the menu degrades richness, never correctness."""
    assert answer_menu_of({}) is None
    assert answer_menu_of(None) is None
    assert answer_menu_of("nope") is None
    assert answer_menu_of({"answer_menu": "torn"}) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
