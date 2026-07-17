"""Mutation-hardening for the numeric-claim auditor (``verify_relay``).

``tests/ops/test_verify_relay.py`` is a comprehensive behavior suite driven
through the ``verify_relay`` verb. This file ADDS direct, boundary-level pins on
the PURE helper functions the verb composes — the number tokenizer, the
match/round/truncate compare, the run/job-id classifiers, the auth-id join
(including the ``settle-aggregate`` guard predicate), the verification-evidence
false-positive guards, the state classifier, the canary/log-quote skips, the
mismatch dedup, and the notebook tri-state ownership. These are the operators
and boundaries a surviving mutation would flip silently; the number-honesty
engine is where a wrong number sneaks through, so each is pinned at the edge.

Every assertion notes the mutation it kills inline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._wire.queries.verify_relay import RelayMismatch, VerifyRelayInput
from hpc_agent.ops.decision.journal.verify_relay import (
    _NB_AMBIGUOUS,
    _NB_OWN,
    _NB_SIBLING,
    _classify_state,
    _collect_source_numbers,
    _count_quantifier,
    _dedupe_mismatches,
    _extract_number_word_claims,
    _id_matches,
    _is_canary_adjacent,
    _is_id_shaped,
    _is_identifier_like,
    _is_log_quote_context,
    _is_number_literal,
    _is_path_key,
    _is_run_id_like,
    _key_evidences_needle,
    _nb_claim_ownership,
    _nearest_number,
    _overlaps,
    _truncate_display,
    _value_token_evidences_needle,
    match_number,
    normalize_num,
    verify_relay,
)
from hpc_agent.state.decision_journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path


# ── normalize_num: strip grouping commas + a trailing % ─────────────────────────


def test_normalize_num_strips_commas_and_percent() -> None:
    assert normalize_num("1,000,000") == "1000000"  # kills: dropping .replace(",","")
    assert normalize_num("95%") == "95"  # kills: dropping .rstrip("%")
    assert normalize_num("3.14") == "3.14"  # unchanged


# ── _is_number_literal: the ONE grammar, anchored ───────────────────────────────


def test_is_number_literal_accepts_the_full_vocabulary() -> None:
    for lit in ("3.14", "1,000", "-15.4283", "4.585623e-11", "95%", "128"):
        assert _is_number_literal(lit) is True, lit


def test_is_number_literal_rejects_ids_and_partial_spans() -> None:
    # kills: fullmatch -> search (a numeric PREFIX of a larger token is NOT a literal).
    assert _is_number_literal("run-1") is False
    assert _is_number_literal("4.585623e") is False
    assert _is_number_literal("2026-07-03") is False


# ── _is_identifier_like: id vs number vs count-phrase token ──────────────────────


def test_identifier_like_true_for_ids() -> None:
    assert _is_identifier_like("run-1") is True
    assert _is_identifier_like("20260703-141500-ab") is True


def test_identifier_like_false_for_numeric_literal_string() -> None:
    # kills: dropping the ``not _is_number_literal`` carve-out (bug-sweep #39 —
    # a negative metric stored as "-3.5" belongs in the number pool).
    assert _is_identifier_like("-3.5") is False


def test_identifier_like_false_without_digit_or_hyphen() -> None:
    assert _is_identifier_like("core-hours") is False  # hyphen, no digit
    assert _is_identifier_like("300") is False  # digit, no hyphen


# ── _is_id_shaped: id-STRENGTH signal, not merely hyphen+digit ───────────────────


def test_id_shaped_true_on_mixed_segment_or_long_digit_run() -> None:
    assert _is_id_shaped("d363e2a3") is True  # a segment mixing letters+digits
    assert _is_id_shaped("2026") is True  # a >=4 digit run


def test_id_shaped_false_for_count_and_english_compounds() -> None:
    # kills: the run-13 finding-8 narrowing (count phrases must not read id-shaped).
    assert _is_id_shaped("300-task") is False
    assert _is_id_shaped("run-level") is False


def test_id_shaped_digit_run_boundary_is_four() -> None:
    # kills: ``\d{4,}`` -> ``\d{3,}`` / ``\d{5,}``.
    assert _is_id_shaped("999") is False
    assert _is_id_shaped("9999") is True


# ── _is_run_id_like ─────────────────────────────────────────────────────────────


def test_run_id_like_scope_and_run_prefix() -> None:
    assert _is_run_id_like("pi-train-d363e2a3", "pi-train-d363e2a3") is True  # scope match
    assert _is_run_id_like("run-2", "scope") is True  # run- + digit suffix
    assert _is_run_id_like("run-level", "scope") is False  # run- + no digit (compound)


def test_run_id_like_number_literal_is_never_a_run_id() -> None:
    # kills: dropping the numeric-literal short-circuit (run-12 finding 29).
    assert _is_run_id_like("4.585623e-11", "scope") is False
    assert _is_run_id_like("2026-07-03", "scope") is False  # ISO date, not a run-id


def test_run_id_like_requires_id_shape_and_length() -> None:
    # kills: the ``len(token) >= 8`` floor / the _is_id_shaped conjunction.
    assert _is_run_id_like("job-x9zzzz1", "scope") is True  # id-shaped, len>=8
    assert _is_run_id_like("300-task", "scope") is False  # not id-shaped


# ── _id_matches: exact or shared >=4-char prefix ────────────────────────────────


def test_id_matches_exact() -> None:
    assert _id_matches("run-1", {"run-1"}) is True


def test_id_matches_shared_prefix_either_direction() -> None:
    assert _id_matches("d363e2a3ffff", {"d363e2a3"}) is True  # token extends aid
    assert _id_matches("d363", {"d363e2a3"}) is True  # aid extends token


def test_id_matches_short_prefix_below_four_is_not_a_match() -> None:
    # kills: ``len(token) >= 4`` -> ``>= 3`` (a 3-char coincidence must not match).
    assert _id_matches("d36", {"d363e2a3"}) is False


# ── match_number: the exact/float/prefix/round/truncate compare ─────────────────


def test_match_number_exact_string() -> None:
    assert match_number("1000", {"1000"}, []) is True


def test_match_number_float_equality() -> None:
    # kills: dropping the ``any(f == val)`` float-equality (95 == 95.0).
    assert match_number("95", set(), [95.0]) is True


def test_match_number_integer_mismatch_returns_false_via_no_dot_guard() -> None:
    # kills: dropping the ``"." not in norm`` early-out (a non-matching int must
    # return False, never fall through to the fractional split).
    assert match_number("256", {"128"}, [128.0]) is False


def test_match_number_prefix_truncation() -> None:
    # kills: the ``len(s) > len(norm) and s.startswith(norm)`` truncation tolerance.
    assert match_number("3.14", {"3.1411"}, [3.1411]) is True


def test_match_number_display_rounding_is_sign_insensitive_for_unsigned() -> None:
    # kills: dropping the ``abs(f)`` candidate for an UNSIGNED claim — a 2dp
    # render "15.43" must reconcile a negative source -15.4283 (em-dash minus).
    assert match_number("15.43", set(), [-15.4283]) is True


def test_match_number_rounding_that_changes_a_digit_is_flagged() -> None:
    # kills: weakening the round/truncate compare to accept any nearby value.
    assert match_number("3.15", {"3.1411"}, [3.1411]) is False


def test_match_number_unparseable_token_is_not_flagged() -> None:
    # kills: the ValueError branch returning False instead of True (nothing to compare).
    assert match_number("1e", set(), []) is True


# ── _truncate_display: toward-zero truncation with the float nudge ──────────────


def test_truncate_display_toward_zero_both_signs() -> None:
    assert _truncate_display(3.14159, 2) == "3.14"
    assert _truncate_display(-3.14159, 2) == "-3.14"


def test_truncate_display_nudge_absorbs_the_representation_gap() -> None:
    # kills: dropping the ``+= 1e-9`` nudge — 0.29*100 == 28.9999999... in float,
    # which truncates to "0.28" without the sign-aware nudge (a faithful 0.29
    # render of a source 0.29 would then spuriously fail to reconcile).
    assert _truncate_display(0.29, 2) == "0.29"


# ── _nearest_number ─────────────────────────────────────────────────────────────


def test_nearest_number_picks_min_abs_distance_and_renders_integral() -> None:
    # kills: the min-by-abs key + the integral ``.0``-stripping render.
    assert _nearest_number("200", [128.0, 256.0]) == "256"


def test_nearest_number_renders_non_integral_verbatim() -> None:
    assert _nearest_number("3", [2.5]) == "2.5"


def test_nearest_number_none_when_empty_or_unparseable() -> None:
    assert _nearest_number("5", []) is None
    assert _nearest_number("x", [1.0]) is None


# ── _overlaps: half-open span intersection ──────────────────────────────────────


def test_overlaps_true_on_real_intersection() -> None:
    assert _overlaps(0, 5, [(4, 10)]) is True


def test_overlaps_false_on_touching_and_disjoint() -> None:
    # kills: ``start < e and s < end`` -> ``<=`` (touching spans must NOT overlap).
    assert _overlaps(0, 5, [(5, 10)]) is False
    assert _overlaps(0, 5, [(10, 20)]) is False


# ── _dedupe_mismatches: the 4-tuple identity key ────────────────────────────────


def _mm(claim: str, kind: str, detail: str, nearest: str | None) -> RelayMismatch:
    return RelayMismatch(claim=claim, kind=kind, detail=detail, nearest_source_value=nearest)  # type: ignore[arg-type]


def test_dedupe_collapses_identical_mismatches() -> None:
    a = _mm("256", "number", "d", "128")
    b = _mm("256", "number", "d", "128")
    assert len(_dedupe_mismatches([a, b])) == 1


def test_dedupe_keeps_mismatches_differing_only_in_nearest_value() -> None:
    # kills: dropping ``nearest_source_value`` from the dedup key tuple.
    a = _mm("256", "number", "d", "128")
    b = _mm("256", "number", "d", "512")
    assert len(_dedupe_mismatches([a, b])) == 2


# ── _classify_state: the verification / lifecycle tri-branch ────────────────────


def test_classify_state_verified_with_evidence_passes() -> None:
    assert _classify_state("verified", None, None, {"verified"}, False) is None


def test_classify_state_verified_no_evidence_no_sources_is_unverifiable() -> None:
    # kills: the ``run_status_family is None and not has_sources`` guard.
    assert _classify_state("verified", None, None, set(), False) == ("unverifiable", None)


def test_classify_state_verified_no_evidence_but_status_contradicts() -> None:
    assert _classify_state("verified", "failed", "failed", set(), True) == ("state", "failed")


def test_classify_state_lifecycle_match_passes() -> None:
    assert _classify_state("running", "in_flight", "running", set(), True) is None


def test_classify_state_lifecycle_mismatch_flags() -> None:
    assert _classify_state("complete", "failed", "failed", set(), True) == ("state", "failed")


def test_classify_state_lifecycle_no_status_is_unverifiable() -> None:
    assert _classify_state("running", None, None, set(), True) == ("unverifiable", None)


# ── _is_canary_adjacent: the _CANARY_WINDOW=40 boundary ─────────────────────────


def test_canary_adjacent_within_window() -> None:
    # "canary" (6) + 34 spaces => the state word starts at char 40 (inclusive edge).
    text = "canary" + " " * 34 + "failed"
    assert _is_canary_adjacent(text, text.index("failed")) is True


def test_canary_not_adjacent_just_past_window() -> None:
    # kills: the 40-char window (start-41 must fall outside the preceding slice).
    text = "canary" + " " * 35 + "failed"
    assert _is_canary_adjacent(text, text.index("failed")) is False


# ── _is_log_quote_context: log-tag / backtick fence signals ─────────────────────


def test_log_quote_true_on_bracket_tag() -> None:
    text = "[transport] command timeout"
    i = text.index("timeout")
    assert _is_log_quote_context(text, i, i + len("timeout")) is True


def test_log_quote_true_inside_backtick_fence() -> None:
    text = "the log reads `command timeout` now"
    i = text.index("timeout")
    assert _is_log_quote_context(text, i, i + len("timeout")) is True


def test_log_quote_false_in_plain_prose() -> None:
    text = "the run timeout occurred as expected"
    i = text.index("timeout")
    assert _is_log_quote_context(text, i, i + len("timeout")) is False


# ── _count_quantifier: the "0 failed" / "no failed" count regex ─────────────────


def test_count_quantifier_numeric_and_zero_words() -> None:
    assert _count_quantifier("0 failed", len("0 ")) == "0"
    assert _count_quantifier("no failed", len("no ")) == "no"
    assert _count_quantifier("3.0 failed", len("3.0 ")) == "3.0"  # whole decimal, not tail


def test_count_quantifier_none_when_word_stands_alone() -> None:
    assert _count_quantifier("run failed", len("run ")) is None


# ── _extract_number_word_claims: the value>=13 threshold + tilde skip ───────────


def test_number_word_threshold_is_exactly_thirteen() -> None:
    # kills: ``_NUMBER_WORD_MIN_VALUE`` (13) and the ``value < MIN`` operator.
    assert [c[3] for c in _extract_number_word_claims("thirteen tasks")] == [13]
    assert _extract_number_word_claims("twelve tasks") == []


def test_number_word_tilde_prefixed_is_conversational() -> None:
    # kills: dropping the ``~`` chatter skip on the word path.
    assert _extract_number_word_claims("~thirteen minutes") == []


# ── _collect_source_numbers: list-length, bool skip, per-token id skip ──────────


def test_collect_source_numbers_contributes_list_length() -> None:
    # kills: dropping the ``len(obj)`` count contribution (run-#12 "27 SLURM jobs").
    strings: set[str] = set()
    floats: list[float] = []
    _collect_source_numbers([10, 20, 30], strings, floats)
    assert "3" in strings and {"10", "20", "30"} <= strings


def test_collect_source_numbers_skips_bools() -> None:
    # kills: dropping the ``isinstance(obj, bool)`` guard (True/False are not numbers).
    strings: set[str] = set()
    floats: list[float] = []
    _collect_source_numbers(True, strings, floats)
    assert strings == set() and floats == []


def test_collect_source_numbers_skips_id_tokens_per_token() -> None:
    # kills: the run-13 finding-8 per-token id skip — "300" is pooled while the
    # id token "run-128" is skipped whole (its "128" never enters the pool).
    strings: set[str] = set()
    floats: list[float] = []
    _collect_source_numbers("300 tasks over run-128 core-hours", strings, floats)
    assert "300" in strings
    assert "128" not in strings


# ── verification-evidence guards: segment vs substring, exact vs contains ───────


def test_is_path_key_segment_equality_not_substring() -> None:
    assert _is_path_key("remote_path") is True
    assert _is_path_key("summary_file") is True
    # kills: substring test — "profile" ends in "file" but has no "file" SEGMENT.
    assert _is_path_key("profile") is False


def test_value_token_needle_exact_match_only() -> None:
    out: set[str] = set()
    _value_token_evidences_needle("green", out)
    assert "green" in out


def test_value_token_needle_rejects_negation_path_and_embedded_label() -> None:
    # kills: substring -> exact — a negated word, a path token, and an embedded
    # label must NOT vouch for the positive verdict.
    for tok in ("unverified", "green_run.json", "model-verified-v2"):
        out: set[str] = set()
        _value_token_evidences_needle(tok, out)
        assert out == set(), tok


def test_value_token_needle_strips_wrapping_punctuation() -> None:
    # kills: dropping the punctuation strip (a bare "verified." still evidences).
    out: set[str] = set()
    _value_token_evidences_needle("verified.", out)
    assert "verified" in out


def test_key_evidences_needle_segment_and_negation() -> None:
    out: set[str] = set()
    _key_evidences_needle("canary_verified", out)
    assert "verified" in out  # positive compound segment
    neg: set[str] = set()
    _key_evidences_needle("unverified", neg)
    assert neg == set()  # kills: substring key test letting "unverified" vouch


# ── _nb_claim_ownership: the tri-state (own / sibling / ambiguous) ──────────────


def test_ownership_own_when_no_sibling_mention() -> None:
    assert _nb_claim_ownership(10, 15, [(0, 5)], []) == _NB_OWN


def test_ownership_sibling_when_this_id_absent() -> None:
    # kills: the ``mine is None`` -> SIBLING branch.
    assert _nb_claim_ownership(100, 105, [], [(0, 5)]) == _NB_SIBLING


def test_ownership_own_when_this_id_strictly_nearer() -> None:
    # kills: ``mine < sibling`` -> OWN.
    assert _nb_claim_ownership(10, 15, [(8, 9)], [(50, 55)]) == _NB_OWN


def test_ownership_sibling_when_sibling_strictly_nearer() -> None:
    # kills: ``sibling < mine`` -> SIBLING.
    assert _nb_claim_ownership(10, 15, [(50, 55)], [(8, 9)]) == _NB_SIBLING


def test_ownership_ambiguous_on_an_exact_tie() -> None:
    # kills: the equidistant tie -> AMBIGUOUS (finding-5: a tie corrects nothing).
    assert _nb_claim_ownership(10, 15, [(5, 8)], [(17, 20)]) == _NB_AMBIGUOUS


# ── the auth-id join: the settle-aggregate block-predicate guard ────────────────

_SCOPE = "main-run-bbbb2222"
_CONTRIB = "contrib-aaaa1111"


def _run_scope(tmp_path: Path, relay: str) -> Any:
    return verify_relay(
        experiment_dir=tmp_path, spec=VerifyRelayInput(run_id=_SCOPE, relay_text=relay)
    )


def test_settle_aggregate_contributing_ids_via_provenance_are_authorized(tmp_path: Path) -> None:
    # kills: dropping the ``provenance`` holder key from the settle auth-join.
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_SCOPE,
        block="settle-aggregate",
        response="settle the table",
        provenance={"contributing_run_ids": [_CONTRIB]},
    )
    out = _run_scope(tmp_path, f"The operator table derives from {_CONTRIB}.")
    assert [m for m in out.mismatches if m.kind == "run_id" and m.claim == _CONTRIB] == []


def test_non_settle_block_contributing_ids_are_not_authorized(tmp_path: Path) -> None:
    # kills: dropping the ``block != "settle-aggregate"`` guard — a contributing
    # id under a NON-settle block must NOT be blessed into the auth-id set.
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_SCOPE,
        block="submit-s1",
        response="y",
        resolved={"contributing_run_ids": [_CONTRIB]},
    )
    out = _run_scope(tmp_path, f"The operator table derives from {_CONTRIB}.")
    flagged = {m.claim for m in out.mismatches if m.kind == "run_id"}
    assert _CONTRIB in flagged


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
