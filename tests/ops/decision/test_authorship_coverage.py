"""Mutation-hardening for the decision-journal authorship substrate.

The gate submodules (registration / conclusion / challenge / reproduction /
overnight_consent) each have a focused behavior suite, but the TWO shared
primitives every one of them builds on — :func:`_is_bare_ack` (the bare-ack
discriminator) and :func:`_refuse_missing_authorship` (the E2 authorship-missing
marker) — had NO direct boundary battery. A silent bug here lets an
un-authored ``y`` commit a value that appeared only in the agent's proposal, or
strips the marker the MCP elicit-then-retry seam keys on.

This file pins both at the boundary:

* :func:`_is_bare_ack` — every member of the frozen bare-ack lexicon returns
  True; the normalization (lowercase, non-letter squashing, ``+`` collapse,
  strip) is exercised; a genuine typed utterance returns False;
* :func:`_refuse_missing_authorship` — it raises ``SpecInvalid``, preserves the
  message, and attaches EXACTLY ``{"authorship_evidence": "missing"}`` as a
  fresh (non-aliased) ``failure_features`` block.

Each assertion notes the mutation it kills inline.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.ops.decision.journal._shared import (
    _AUTHORSHIP_EVIDENCE_MISSING,
    _BARE_ACK_RESPONSES,
    _is_bare_ack,
    _refuse_missing_authorship,
)

# ── _is_bare_ack: every lexicon member is a bare ack ────────────────────────────


@pytest.mark.parametrize("word", sorted(_BARE_ACK_RESPONSES))
def test_every_lexicon_member_is_a_bare_ack(word: str) -> None:
    # kills: removing ANY entry from ``_BARE_ACK_RESPONSES`` — each member must
    # still be recognised as a bare ack (an accidental drop would let that word
    # commit an agent-proposed value).
    assert _is_bare_ack(word) is True


def test_uppercase_ack_is_normalised() -> None:
    # kills: dropping ``.lower()`` in the normalization.
    assert _is_bare_ack("YES") is True
    assert _is_bare_ack("Y") is True
    assert _is_bare_ack("LGTM") is True


def test_surrounding_whitespace_is_stripped() -> None:
    # kills: dropping ``.strip()`` (a padded ``y`` must still be a bare ack).
    assert _is_bare_ack("   y   ") is True


def test_punctuation_is_squashed_to_a_bare_ack() -> None:
    # kills: mutating the ``[^a-z]+`` substitution char-class (punctuation and
    # digits around/between ack letters must not defeat recognition).
    assert _is_bare_ack("ok!") is True
    assert _is_bare_ack("go ahead!!") is True
    assert _is_bare_ack("do it.") is True


def test_multiple_interior_separators_collapse() -> None:
    # kills: dropping the ``+`` quantifier in ``[^a-z]+`` — multiple interior
    # non-letters must collapse to ONE space so the phrase still matches the
    # single-spaced lexicon entry.
    assert _is_bare_ack("go    ahead") is True
    assert _is_bare_ack("sounds---good") is True


def test_genuine_typed_utterance_is_not_a_bare_ack() -> None:
    # kills: flipping the ``in`` membership to ``not in`` / weakening the gate —
    # a freshly authored utterance that NAMES content must pass the ack test as
    # NOT-bare so the authorship gate lets it through.
    assert _is_bare_ack("y submit-s2 pi-train-d363e2a3 @a1b2c3d4") is False
    assert _is_bare_ack("the metric settled at 3.14 across 10 tasks") is False
    assert _is_bare_ack("approve the causal_tune_tree table") is False


def test_empty_and_whitespace_only_are_not_bare_acks() -> None:
    # kills: the ``(response or "")`` guard / an over-broad match — an EMPTY
    # utterance is not an ack (it carries no acknowledgement token), and it must
    # not be treated as one.
    assert _is_bare_ack("") is False
    assert _is_bare_ack("   ") is False


def test_ack_word_embedded_in_a_longer_utterance_is_not_bare() -> None:
    # kills: swapping the exact-membership test for a substring/startswith test —
    # "yes, use 256 core-hours" is a content-bearing utterance, not a bare ack.
    assert _is_bare_ack("yes, use 256 core-hours") is False
    assert _is_bare_ack("ok run it on hoffman2 with 50 seeds") is False


# ── _refuse_missing_authorship: the E2 marker raise ─────────────────────────────


def test_refuse_raises_spec_invalid_with_the_message() -> None:
    # kills: changing the exception TYPE or dropping the message passthrough.
    with pytest.raises(errors.SpecInvalid) as exc:
        _refuse_missing_authorship("the human's authorship evidence is missing")
    assert "authorship evidence is missing" in str(exc.value)


def test_refuse_attaches_exact_authorship_missing_marker() -> None:
    # kills: mutating the marker KEY or VALUE — the MCP elicit-then-retry hook
    # keys on the distinct ``authorship_evidence`` key, so the block must be
    # EXACTLY {"authorship_evidence": "missing"}.
    with pytest.raises(errors.SpecInvalid) as exc:
        _refuse_missing_authorship("bare ack")
    features = exc.value.failure_features  # type: ignore[attr-defined]
    assert features == {"authorship_evidence": "missing"}


def test_refuse_marker_is_a_fresh_copy_not_the_module_constant() -> None:
    # kills: ``dict(_AUTHORSHIP_EVIDENCE_MISSING)`` -> aliasing the module
    # constant (a per-exception copy must not share identity with, or mutate,
    # the shared template).
    with pytest.raises(errors.SpecInvalid) as exc:
        _refuse_missing_authorship("bare ack")
    features = exc.value.failure_features  # type: ignore[attr-defined]
    assert features == _AUTHORSHIP_EVIDENCE_MISSING
    assert features is not _AUTHORSHIP_EVIDENCE_MISSING


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
