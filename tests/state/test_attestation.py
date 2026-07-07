"""Tests for the attestation kernel (``state/attestation.py``).

Pins the three shared functions every trusted-record instance routes through:
the record-shape validator (fires + passes), :func:`bind`'s recompute lock
(refuses a mismatched sha, passes a matching one, callable + literal recompute),
and :func:`reduce`'s newest-first drift-revocation (newest wins, stale on drift,
absent on empty, per-subject filtering). Boundary: opaque kinds, no invented
subject ids, tolerant read.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.state import attestation
from hpc_agent.state.attestation import ABSENT, CURRENT, STALE, Attestation


def _rec(**overrides: object) -> dict[str, object]:
    """A minimal valid attestation record, overridable per field."""
    base: dict[str, object] = {
        "attestor": "human",
        "subject_kind": "notebook-section",
        "subject_id": "audit-7:load-data",
        "content_sha": "sha-a",
    }
    base.update(overrides)
    return base


# ── validate: fires + passes ──────────────────────────────────────────────────


def test_validate_passes_a_minimal_record() -> None:
    att = attestation.validate(_rec())
    assert isinstance(att, Attestation)
    assert att.attestor == "human"
    assert att.subject_kind == "notebook-section"
    assert att.subject_id == "audit-7:load-data"
    assert att.content_sha == "sha-a"
    assert att.view_sha is None
    assert att.evidence is None


def test_validate_carries_optional_view_sha_and_evidence() -> None:
    att = attestation.validate(_rec(attestor="code", view_sha="view-1", evidence={"lint": "green"}))
    assert att.attestor == "code"
    assert att.view_sha == "view-1"
    assert att.evidence == {"lint": "green"}


@pytest.mark.parametrize("bad", ["approver", "llm", "", "Human", None, 1])
def test_validate_refuses_a_non_literal_attestor(bad: object) -> None:
    with pytest.raises(errors.SpecInvalid):
        attestation.validate(_rec(attestor=bad))


@pytest.mark.parametrize("field", ["attestor", "subject_kind", "subject_id", "content_sha"])
def test_validate_refuses_a_missing_required_field(field: str) -> None:
    record = _rec()
    del record[field]
    with pytest.raises(errors.SpecInvalid):
        attestation.validate(record)


@pytest.mark.parametrize("field", ["subject_kind", "subject_id", "content_sha"])
def test_validate_refuses_an_empty_required_field(field: str) -> None:
    # Core never invents a subject_id (or any required field) — an empty one is
    # a refusal, not a silent default.
    with pytest.raises(errors.SpecInvalid):
        attestation.validate(_rec(**{field: ""}))


@pytest.mark.parametrize("field", ["subject_kind", "subject_id", "content_sha"])
def test_validate_refuses_a_non_string_required_field(field: str) -> None:
    with pytest.raises(errors.SpecInvalid):
        attestation.validate(_rec(**{field: 123}))


@pytest.mark.parametrize("bad", ["", 5, ["view"]])
def test_validate_refuses_a_present_but_bad_view_sha(bad: object) -> None:
    with pytest.raises(errors.SpecInvalid):
        attestation.validate(_rec(view_sha=bad))


def test_validate_refuses_a_non_mapping() -> None:
    with pytest.raises(errors.SpecInvalid):
        attestation.validate("not-a-record")  # type: ignore[arg-type]


def test_subject_kind_is_opaque() -> None:
    # Any non-empty string is a legal kind — the kernel attaches no vocabulary.
    for kind in ("greenlight", "scope", "reproduction-receipt", "totally-made-up"):
        assert attestation.validate(_rec(subject_kind=kind)).subject_kind == kind


# ── bind: recompute-and-compare lock ──────────────────────────────────────────


def test_bind_passes_a_matching_sha_literal() -> None:
    att = attestation.bind(_rec(content_sha="sha-a"), recompute="sha-a")
    assert att.content_sha == "sha-a"


def test_bind_passes_a_matching_sha_callable() -> None:
    att = attestation.bind(_rec(content_sha="sha-a"), recompute=lambda: "sha-a")
    assert att.content_sha == "sha-a"


def test_bind_refuses_a_mismatched_sha() -> None:
    # The un-fakeable lock: an asserted hash that does not match the recompute
    # cannot be appended (D5 lock 2).
    with pytest.raises(errors.SpecInvalid, match="does not match the recomputed"):
        attestation.bind(_rec(content_sha="sha-asserted"), recompute="sha-actual")


def test_bind_refuses_when_recompute_yields_no_sha() -> None:
    with pytest.raises(errors.SpecInvalid):
        attestation.bind(_rec(), recompute="")


def test_bind_validates_shape_before_comparing() -> None:
    # A malformed record is refused by the shape check even if a recompute is
    # supplied — bind is validate + compare, never compare alone.
    with pytest.raises(errors.SpecInvalid):
        attestation.bind(_rec(attestor="nobody"), recompute="sha-a")


# ── reduce: newest-first drift-revocation ─────────────────────────────────────


def test_reduce_empty_is_absent() -> None:
    assert attestation.reduce([], current_sha="sha-a") == ABSENT


def test_reduce_current_when_newest_matches() -> None:
    records = [_rec(content_sha="sha-a")]
    assert attestation.reduce(records, current_sha="sha-a") == CURRENT


def test_reduce_stale_on_drift() -> None:
    # The subject moved to sha-b; the attestation at sha-a reads stale.
    records = [_rec(content_sha="sha-a")]
    assert attestation.reduce(records, current_sha="sha-b") == STALE


def test_reduce_newest_wins_over_older_records() -> None:
    # Append order: oldest first. The newest (last) record decides. Here the
    # human re-signed at sha-b after an edit → current.
    records = [_rec(content_sha="sha-a"), _rec(content_sha="sha-b")]
    assert attestation.reduce(records, current_sha="sha-b") == CURRENT


def test_reduce_newest_stale_even_when_an_older_record_matched() -> None:
    # A stale newest attestation is not rescued by an older matching one — the
    # newest touchpoint is the current state (re-edit after a sign-off).
    records = [_rec(content_sha="sha-b"), _rec(content_sha="sha-a")]
    assert attestation.reduce(records, current_sha="sha-b") == STALE


def test_reduce_filters_by_subject_id() -> None:
    records = [
        _rec(subject_id="sec-1", content_sha="sha-1"),
        _rec(subject_id="sec-2", content_sha="sha-2"),
    ]
    assert attestation.reduce(records, current_sha="sha-1", subject_id="sec-1") == CURRENT
    assert attestation.reduce(records, current_sha="sha-2", subject_id="sec-2") == CURRENT
    # A subject with no records among the mixed sequence is absent.
    assert attestation.reduce(records, current_sha="sha-9", subject_id="sec-9") == ABSENT


def test_reduce_skips_malformed_records() -> None:
    # Tolerant read: a corrupt record never strands the reduction.
    records: list[dict[str, object]] = [
        {"attestor": "human"},  # missing required fields → skipped
        _rec(content_sha="sha-a"),
    ]
    assert attestation.reduce(records, current_sha="sha-a") == CURRENT


def test_reduce_all_malformed_is_absent() -> None:
    bogus: list[dict[str, object]] = [{"bogus": 1}, {"attestor": "code"}]
    assert attestation.reduce(bogus, current_sha="sha-a") == ABSENT


def test_reduce_view_sha_is_optional_end_to_end() -> None:
    # view_sha rides along but never affects the drift verdict — it records what
    # the human saw, not what content was covered.
    with_view = [_rec(content_sha="sha-a", view_sha="view-1")]
    without_view = [_rec(content_sha="sha-a")]
    assert attestation.reduce(with_view, current_sha="sha-a") == CURRENT
    assert attestation.reduce(without_view, current_sha="sha-a") == CURRENT
