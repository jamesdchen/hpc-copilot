"""Mutation-hardening for the code-composed OFFERED-CONSENT hint composer.

``tests/ops/test_approve_hint.py`` pins the happy-path utterance grammar and the
gate-acceptance contract. This battery ADDS the boundary/operator/default pins
the mutation program flagged as un-exercised on
``_kernel/lifecycle/consent_hint.py`` (the consent seam — a silent bug here lets
an unauthorized settle through under an invisible scope):

* :func:`brief_cluster` — every shape + fall-through (NO prior test at all);
* :func:`_sha8` — the exact 8-hex slice + the empty-string sink;
* :func:`compose_approve_hint` — the empty-``successor`` sink, the
  ``cluster`` / ``next_spec_sha`` truthiness gates on ``scope_tokens``, the
  standing ``workflow == "campaign"`` predicate + its ELSE branch (overnight),
  the ``note`` "nothing auto-fills / relay VERBATIM / bare y accepted"
  invariants (item 2: never auto-fills), and the sha-absent / bounds-absent
  standing-line composition;
* :func:`_standing_bound_clauses` — the ``> 0`` cap floors, the bool-guard, the
  wake truthiness, the clause text + ``:g`` format + ordering.

Each assertion notes the mutation it kills in an inline comment.
"""

from __future__ import annotations

from typing import Any

import pytest

from hpc_agent._kernel.lifecycle.consent_hint import (
    _sha8,
    _standing_bound_clauses,
    brief_cluster,
    compose_approve_hint,
)

_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"


# ── brief_cluster: the cluster a brief carries across the known shapes ──────────


def test_brief_cluster_direct_key() -> None:
    # kills: dropping the top-level ``cluster`` read.
    assert brief_cluster({"cluster": "hoffman2"}) == "hoffman2"


def test_brief_cluster_from_resolved_block() -> None:
    # kills: dropping the ``resolved.cluster`` branch.
    assert brief_cluster({"resolved": {"cluster": "carc"}}) == "carc"


def test_brief_cluster_from_resolve_submit_spec() -> None:
    # kills: dropping the ``resolve.submit_spec.cluster`` branch.
    assert brief_cluster({"resolve": {"submit_spec": {"cluster": "carc"}}}) == "carc"


def test_brief_cluster_none_when_absent() -> None:
    # kills: the final ``return None`` mutated to a truthy value.
    assert brief_cluster({}) is None


def test_brief_cluster_direct_wins_over_nested() -> None:
    # kills: reordering the reads (direct must be consulted first).
    assert brief_cluster({"cluster": "direct", "resolved": {"cluster": "nested"}}) == "direct"


def test_brief_cluster_empty_direct_falls_through_to_nested() -> None:
    # kills: dropping the ``and val`` truthiness guard on the direct read (an
    # empty string must NOT short-circuit; the nested value must win).
    assert brief_cluster({"cluster": "", "resolved": {"cluster": "carc"}}) == "carc"


def test_brief_cluster_non_string_direct_is_not_returned() -> None:
    # kills: dropping ``isinstance(val, str)`` on the direct read (a truthy int
    # would leak through a bare ``if val``).
    assert brief_cluster({"cluster": 123}) is None


def test_brief_cluster_empty_nested_is_ignored() -> None:
    # kills: dropping the innermost ``and cluster`` in the resolve.submit_spec arm.
    assert brief_cluster({"resolve": {"submit_spec": {"cluster": ""}}}) is None


def test_brief_cluster_non_dict_resolved_does_not_crash() -> None:
    # kills: dropping ``isinstance(resolved, dict)`` (would AttributeError / leak).
    assert brief_cluster({"resolved": "not-a-dict"}) is None


# ── _sha8: the 8-hex display prefix ────────────────────────────────────────────


def test_sha8_takes_exactly_eight_hex() -> None:
    # kills: ``[:8]`` -> ``[:7]`` / ``[:9]`` (slice boundary).
    assert _sha8(_SHA) == "a1b2c3d4"
    assert len(_sha8(_SHA)) == 8  # type: ignore[arg-type]


def test_sha8_empty_string_is_none() -> None:
    # kills: dropping the ``and sha`` guard ("" -> "" would be falsy but the
    # explicit None is the contract the caller keys ``if sha8`` on).
    assert _sha8("") is None


def test_sha8_none_is_none() -> None:
    # kills: dropping the ``isinstance(sha, str)`` guard.
    assert _sha8(None) is None


# ── compose_approve_hint: sinks + scope-token truthiness gates ──────────────────


def test_empty_successor_yields_no_hint() -> None:
    # kills: ``isinstance(successor, str) and successor`` -> dropping ``and successor``.
    assert compose_approve_hint(workflow="submit", successor="", run_id="r1") is None


def test_empty_cluster_omitted_from_scope_tokens() -> None:
    # kills: dropping ``and cluster`` on the scope_tokens cluster write.
    hint = compose_approve_hint(workflow="submit", successor="submit-s2", run_id="r1", cluster="")
    assert hint is not None
    assert "cluster" not in hint["scope_tokens"]


def test_none_cluster_omitted_from_scope_tokens() -> None:
    # kills: dropping ``isinstance(cluster, str)`` on the scope_tokens cluster write.
    hint = compose_approve_hint(workflow="submit", successor="submit-s2", run_id="r1")
    assert hint is not None
    assert "cluster" not in hint["scope_tokens"]


def test_empty_sha_omits_pin_from_utterance_and_tokens() -> None:
    # kills: dropping ``and next_spec_sha`` (an empty sha must add no @pin token
    # and no scope_tokens entry).
    hint = compose_approve_hint(
        workflow="submit", successor="submit-s2", run_id="r1", next_spec_sha=""
    )
    assert hint is not None
    assert hint["utterance"] == "y submit-s2 r1"
    assert "@" not in hint["utterance"]
    assert "next_spec_sha" not in hint["scope_tokens"]


def test_note_pins_display_only_invariants() -> None:
    # kills: mutating the ``note`` (item 2: never auto-fills; relay verbatim; bare
    # y accepted). These are the OFFERED-CONSENT guarantees the seam rests on.
    hint = compose_approve_hint(
        workflow="submit", successor="submit-s2", run_id="r1", next_spec_sha=_SHA
    )
    assert hint is not None
    assert hint["bare_ok"] is True
    note = hint["note"]
    assert "nothing auto-fills it" in note
    assert "VERBATIM" in note
    assert "bare 'y' remains accepted" in note


def test_non_standing_line_names_the_sha_pin_clause() -> None:
    # kills: dropping the sha branch of ``pin_clause`` / the trailing bare-y line.
    hint = compose_approve_hint(
        workflow="submit", successor="submit-s2", run_id="r1", next_spec_sha=_SHA
    )
    assert hint is not None
    assert "the code-composed submit-s2 spec (sha a1b2c3d4)" in hint["line"]
    assert 'A bare "y" still works.' in hint["line"]


def test_non_standing_line_without_sha_drops_the_sha_parenthetical() -> None:
    # kills: swapping the ``if sha8`` arms of ``pin_clause`` (no sha -> no "(sha ...)").
    hint = compose_approve_hint(workflow="submit", successor="submit-s2", run_id="r1")
    assert hint is not None
    assert "the code-composed submit-s2 spec" in hint["line"]
    assert "(sha" not in hint["line"]


# ── standing consent: the campaign vs overnight predicate + line composition ────


def test_standing_non_campaign_workflow_is_overnight_subject() -> None:
    # kills: the ``workflow == "campaign"`` predicate / its ELSE branch — a
    # non-campaign standing consent must read "unattended overnight advances".
    hint = compose_approve_hint(
        workflow="submit", successor="submit-s3", run_id="r1", standing=True
    )
    assert hint is not None
    assert "unattended overnight advances" in hint["line"]
    assert "unattended async campaign" not in hint["line"]


def test_standing_none_workflow_is_overnight_subject() -> None:
    # kills: the ``workflow or ""`` default (None must not read as "campaign").
    hint = compose_approve_hint(workflow=None, successor="submit-s3", run_id="r1", standing=True)
    assert hint is not None
    assert "unattended overnight advances" in hint["line"]


def test_standing_campaign_workflow_is_campaign_subject() -> None:
    # kills: the ``== "campaign"`` predicate (positive side) — pairs with the
    # overnight test above so BOTH branches are pinned.
    hint = compose_approve_hint(
        workflow="campaign", successor="campaign-watch", run_id="c1", standing=True
    )
    assert hint is not None
    assert "unattended async campaign" in hint["line"]


def test_standing_line_without_sha_omits_the_pin() -> None:
    # kills: dropping the ``if sha8`` guard on the standing line's pin clause.
    hint = compose_approve_hint(
        workflow="campaign", successor="campaign-watch", run_id="c1", standing=True
    )
    assert hint is not None
    assert "code-composed" not in hint["line"]


def test_standing_line_without_bounds_omits_the_bounds_clause() -> None:
    # kills: dropping the ``if bound_clauses`` guard on the standing line.
    hint = compose_approve_hint(
        workflow="campaign", successor="campaign-watch", run_id="c1", standing=True
    )
    assert hint is not None
    assert "its bounds" not in hint["line"]


def test_standing_line_with_bounds_names_them() -> None:
    # kills: dropping the bounds-clause append when bounds ARE present.
    hint = compose_approve_hint(
        workflow="campaign",
        successor="campaign-watch",
        run_id="c1",
        standing=True,
        bounds={"expires_at": "2026-07-17T08:00:00+00:00"},
    )
    assert hint is not None
    assert "its bounds (until 2026-07-17T08:00:00+00:00)" in hint["line"]


# ── _standing_bound_clauses: the caps floors, bool-guard, wake, format, order ───


def test_bound_clauses_non_dict_is_empty() -> None:
    # kills: dropping ``isinstance(bounds, dict)``.
    assert _standing_bound_clauses(None) == ([], {})
    assert _standing_bound_clauses("nope") == ([], {})  # type: ignore[arg-type]


def test_bound_clauses_expires_prefix_and_token() -> None:
    # kills: the "until " prefix literal + the token map key.
    clauses, tokens = _standing_bound_clauses({"expires_at": "2026-07-17T08:00:00+00:00"})
    assert clauses == ["until 2026-07-17T08:00:00+00:00"]
    assert tokens == {"expires_at": "2026-07-17T08:00:00+00:00"}


def test_bound_clauses_empty_expires_ignored() -> None:
    # kills: dropping the ``and expires`` truthiness guard.
    assert _standing_bound_clauses({"expires_at": ""}) == ([], {})


def test_bound_clauses_walltime_clause_and_g_format() -> None:
    # kills: the "≤ ... wall-seconds" literal + the ``:g`` format.
    clauses, tokens = _standing_bound_clauses({"walltime_cap": 36000})
    assert clauses == ["≤ 36000 wall-seconds"]
    assert tokens == {"walltime_cap": 36000}


def test_bound_clauses_walltime_g_format_drops_integral_float_tail() -> None:
    # kills: ``:g`` -> ``:f`` / removal (36000.0 must render "36000", not "36000.0").
    clauses, _ = _standing_bound_clauses({"walltime_cap": 36000.0})
    assert clauses == ["≤ 36000 wall-seconds"]


def test_bound_clauses_zero_walltime_excluded() -> None:
    # kills: ``walltime > 0`` -> ``>= 0`` (a 0 cap is not a real bound).
    assert _standing_bound_clauses({"walltime_cap": 0}) == ([], {})


def test_bound_clauses_negative_walltime_excluded() -> None:
    # reinforces the ``> 0`` floor against a sign mutation.
    assert _standing_bound_clauses({"walltime_cap": -5}) == ([], {})


def test_bound_clauses_bool_walltime_excluded() -> None:
    # kills: dropping ``not isinstance(walltime, bool)`` (True is int 1 > 0).
    assert _standing_bound_clauses({"walltime_cap": True}) == ([], {})


def test_bound_clauses_budget_clause() -> None:
    # kills: the "≤ ... budget" literal + the budget token.
    clauses, tokens = _standing_bound_clauses({"budget_cap": 12.5})
    assert clauses == ["≤ 12.5 budget"]
    assert tokens == {"budget_cap": 12.5}


def test_bound_clauses_zero_budget_excluded() -> None:
    # kills: ``budget > 0`` -> ``>= 0``.
    assert _standing_bound_clauses({"budget_cap": 0}) == ([], {})


def test_bound_clauses_bool_budget_excluded() -> None:
    # kills: dropping ``not isinstance(budget, bool)`` on the budget arm.
    assert _standing_bound_clauses({"budget_cap": True}) == ([], {})


def test_bound_clauses_wake_truthy_named() -> None:
    # kills: dropping the "wake armed" clause / the wake token.
    clauses, tokens = _standing_bound_clauses({"wake": {"kind": "watch"}})
    assert clauses == ["wake armed"]
    assert tokens == {"wake": {"kind": "watch"}}


def test_bound_clauses_falsy_wake_ignored() -> None:
    # kills: ``if wake`` -> unconditional (None / 0 / "" must add no clause).
    falsies: list[Any] = [None, 0, "", []]
    for falsy in falsies:
        assert _standing_bound_clauses({"wake": falsy}) == ([], {})


def test_bound_clauses_ordering_is_stable() -> None:
    # kills: reordering the four clause appends (duration, wall, budget, wake).
    bounds: dict[str, Any] = {
        "expires_at": "2026-07-17T08:00:00+00:00",
        "walltime_cap": 36000,
        "budget_cap": 12.5,
        "wake": {"kind": "watch"},
    }
    clauses, _ = _standing_bound_clauses(bounds)
    assert clauses == [
        "until 2026-07-17T08:00:00+00:00",
        "≤ 36000 wall-seconds",
        "≤ 12.5 budget",
        "wake armed",
    ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
