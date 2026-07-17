"""Behaviour-pinning coverage for :mod:`hpc_agent.state.attestation`.

The 2026-07-17 mutation triage-2 (``docs/plans/mutation-triage-2-2026-07-17.md``)
found the curated matrix ran DARK — attestation produced zero mutation verdicts,
so nothing confirmed which surviving boundary/operator/default mutants the suite
actually kills. attestation is the recompute-lock kernel every trusted record
rides (bind / reduce / validate): a silent mutation here is a silent TRUST bug —
a hash asserted into existence, a stale sign-off read as current, an empty
subject_id fabricated.

``tests/state/test_attestation.py`` already pins the happy paths and the headline
refusals; this file ADDS the boundary/operator/ordering pins those tests leave a
mutant free to survive: the frozenset/constant identities, the ``get(...)`` (no
truthy-default) evidence read, the validate-BEFORE-recompute ordering, the
callable no-sha refusal, and the cross-subject / per-subject reducer behaviours.

Every assertion notes the mutation it kills inline.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.state import attestation
from hpc_agent.state.attestation import (
    _REQUIRED_STR_FIELDS,
    ABSENT,
    ATTESTORS,
    CURRENT,
    STALE,
)


def _rec(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "attestor": "human",
        "subject_kind": "notebook-section",
        "subject_id": "audit-7:load-data",
        "content_sha": "sha-a",
    }
    base.update(overrides)
    return base


# ── module constants: the verdict literals + the attestor/required identities ──


def test_verdict_constants_are_the_exact_distinct_literals() -> None:
    # kills: swapping any of CURRENT/STALE/ABSENT to another string — reduce()
    # returns these verbatim, so a stranger's greenlight tooling keys on them.
    assert (CURRENT, STALE, ABSENT) == ("current", "stale", "absent")
    assert len({CURRENT, STALE, ABSENT}) == 3  # all distinct


def test_attestors_are_exactly_human_and_code() -> None:
    # kills: adding/removing an ATTESTORS member (e.g. widening to accept
    # "approver", or dropping "code"). BOTH literals must validate; nothing else.
    assert frozenset({"human", "code"}) == ATTESTORS
    assert attestation.validate(_rec(attestor="human")).attestor == "human"
    assert attestation.validate(_rec(attestor="code")).attestor == "code"


def test_required_str_fields_is_the_exact_four_tuple() -> None:
    # kills: dropping a field from the required-non-empty-string set — each of
    # these is anti-fabrication load-bearing (a missing subject_id is refused,
    # never invented).
    assert _REQUIRED_STR_FIELDS == ("attestor", "subject_kind", "subject_id", "content_sha")


# ── validate: the get()-not-default reads + tolerant shape ─────────────────────


@pytest.mark.parametrize("ev", [0, "", [], False, {}])
def test_validate_preserves_a_falsy_but_present_evidence(ev: object) -> None:
    # kills: ``evidence=record.get("evidence") or None`` — evidence is OPAQUE, so
    # a falsy-but-present payload (a 0 count, an empty list) must survive as-is,
    # not be silently coerced to None by a truthy default.
    assert attestation.validate(_rec(evidence=ev)).evidence == ev


def test_validate_default_evidence_is_none_when_absent() -> None:
    # kills: a non-None default in ``record.get("evidence")`` — absent → None.
    assert attestation.validate(_rec()).evidence is None


def test_validate_ignores_unknown_keys() -> None:
    # kills: a shape check that rejects extra keys — validate reads only the known
    # fields off the journal record (tolerant read), it is not a closed schema.
    att = attestation.validate(_rec(unrelated_field="x", another=42))
    assert att.subject_id == "audit-7:load-data"


# ── bind: validate-before-recompute ordering + the callable no-sha refusal ─────


def test_bind_validates_shape_before_calling_recompute() -> None:
    # kills: reordering bind so the (possibly expensive) recompute runs before the
    # cheap shape check — a malformed record must be refused WITHOUT the callable
    # ever being invoked (the docstring's deferred-recompute contract).
    calls = {"n": 0}

    def _recompute() -> str:
        calls["n"] += 1
        return "sha-a"

    with pytest.raises(errors.SpecInvalid):
        attestation.bind(_rec(attestor="nobody"), recompute=_recompute)
    assert calls["n"] == 0  # recompute never ran on the bad-shape record


def test_bind_refuses_a_callable_that_yields_empty() -> None:
    # kills: the no-sha guard only covering the literal form — a callable that
    # returns "" (or None) must be refused too, on the SAME ``not current`` path.
    with pytest.raises(errors.SpecInvalid, match="must yield a non-empty content sha"):
        attestation.bind(_rec(), recompute=lambda: "")


def test_bind_refuses_a_callable_that_yields_none() -> None:
    # kills: dropping the ``not isinstance(current, str)`` half of the guard — a
    # callable returning None is not a sha.
    with pytest.raises(errors.SpecInvalid):
        attestation.bind(_rec(), recompute=lambda: None)  # type: ignore[arg-type,return-value]


def test_bind_returns_the_validated_attestation_carrying_optional_fields() -> None:
    # kills: bind returning a re-derived / stripped object — on a matching sha it
    # returns the SAME validated attestation, optional fields intact.
    att = attestation.bind(
        _rec(content_sha="sha-a", view_sha="v1", attestor_id="alice", evidence={"x": 1}),
        recompute="sha-a",
    )
    assert att.content_sha == "sha-a"
    assert (att.view_sha, att.attestor_id, att.evidence) == ("v1", "alice", {"x": 1})


# ── reduce: cross-subject default + per-subject filter isolation ───────────────


def test_reduce_no_subject_id_takes_the_newest_across_all_subjects() -> None:
    # kills: the ``subject_id is not None`` filter-guard being inverted / dropped.
    # With subject_id unset EVERY record is in scope, so the newest (last-appended)
    # record decides regardless of which subject it belongs to.
    records = [
        _rec(subject_id="s1", content_sha="sha-1"),
        _rec(subject_id="s2", content_sha="sha-2"),
    ]
    # newest overall is s2/sha-2 → matches sha-2 → CURRENT; against sha-1 → STALE.
    assert attestation.reduce(records, current_sha="sha-2") == CURRENT
    assert attestation.reduce(records, current_sha="sha-1") == STALE


def test_reduce_per_subject_is_stale_even_when_a_sibling_subject_is_current() -> None:
    # kills: a filter that leaks a DIFFERENT subject's record into the verdict.
    # s2's newest attests sha-old, so s2 reduces STALE against sha-1 even though
    # s1 (a sibling) is perfectly current at sha-1.
    records = [
        _rec(subject_id="s1", content_sha="sha-1"),
        _rec(subject_id="s2", content_sha="sha-old"),
    ]
    assert attestation.reduce(records, current_sha="sha-1", subject_id="s2") == STALE
    assert attestation.reduce(records, current_sha="sha-1", subject_id="s1") == CURRENT


def test_reduce_skips_a_malformed_newest_and_keeps_the_valid_older_same_subject() -> None:
    # kills: the tolerant-read ``except SpecInvalid: continue`` being removed (a
    # corrupt trailing record would otherwise strand or raise). The older valid
    # record for the subject still decides.
    records: list[dict[str, object]] = [
        _rec(subject_id="s1", content_sha="sha-a"),
        {"attestor": "human", "subject_id": "s1"},  # malformed → skipped
    ]
    assert attestation.reduce(records, current_sha="sha-a", subject_id="s1") == CURRENT


def test_reduce_absent_when_only_other_subjects_have_records() -> None:
    # kills: the per-subject filter degrading to "any record" — a subject with no
    # records among a mixed sequence is ABSENT, not rescued by siblings.
    records = [_rec(subject_id="s1", content_sha="sha-1")]
    assert attestation.reduce(records, current_sha="sha-1", subject_id="s9") == ABSENT
