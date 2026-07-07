"""The attestation kernel — the one primitive every trusted record rides.

Design origin: ``docs/design/notebook-audit.md`` ("NEW TASK T0: the attestation
kernel" + the reuse-accounting paragraph). Greenlights, scope unlocks, scope
locks, sign-offs, reproduction receipts, look records — and the future
registration kernel — are instances of ONE object, the **ATTESTATION**::

    {attestor: "human" | "code",
     subject_kind: <opaque str>,   # what class of thing was attested
     subject_id:   <opaque str>,   # caller-authored id — NEVER core-invented
     content_sha:  <str>,          # the hash of what was attested
     view_sha?:    <str>,          # optional: the projection the human saw
     evidence?:    <opaque>}       # opaque payload, never interpreted here

An attestation is a PROJECTION over an existing decision-journal record (its
``resolved`` / block fields), not a new store: instances read their journal
record, build the small attestation dict, and route through the three functions
below. Nothing here writes a file, migrates a schema, or adds a dependency.

The three functions every instance shares:

* :func:`validate` — the record-shape validator (attestor literal, required
  non-empty fields, opaque kinds). Core never invents ``subject_id`` — it must
  be a non-empty caller-authored string.
* :func:`bind` — recompute-and-compare at append time: the un-fakeable lock,
  extracted once (D5 lock 2). The asserted ``content_sha`` must equal a freshly
  recomputed sha, so a hash cannot be asserted into existence. Applies to BOTH
  human and code attestations (a human sign-off recomputes the section sha just
  as a code receipt recomputes its output sha).
* :func:`reduce` — newest-first drift-revocation, defined once: a sequence of a
  subject's attestation records reduces to :data:`CURRENT` / :data:`STALE` /
  :data:`ABSENT`. A record whose ``content_sha`` no longer matches the subject's
  current sha reads ``STALE`` — an edit revokes stale trust with no state
  machine.

Human vs code attestations are the SAME record shape; they differ only in which
ADDITIONAL lock applies. Code attestations rest on :func:`bind`'s recompute
alone; human attestations ALSO face the per-instance authorship gates that stay
in ``ops/decision/journal.py`` (``_assert_unlock_authorship`` and friends).
Gates stay THIN and PER-INSTANCE and CALL this kernel — this is deliberately
NOT a parametric mega-gate (the instances route ``next_block``, are
directionally asymmetric, or carry tiers in load-bearing ways).

Pure, dependency-free, no I/O: ``state.attestation`` is the substrate the
journal-riding instances share; it never reaches SSH, ``_wire``, or the
filesystem.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent import errors

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

__all__ = [
    "ATTESTORS",
    "CURRENT",
    "STALE",
    "ABSENT",
    "Attestation",
    "validate",
    "bind",
    "reduce",
]

#: The two — and only two — attestors. A human attestation faces the authorship
#: gates; a code attestation faces :func:`bind`'s recompute. The record shape is
#: identical, so the reducer and validator treat them as one object.
ATTESTORS = frozenset({"human", "code"})

#: :func:`reduce` verdicts. CURRENT: the newest attestation still matches the
#: subject's current sha. STALE: it matches an older sha (an edit revoked it).
#: ABSENT: no valid attestation for the subject at all.
CURRENT = "current"
STALE = "stale"
ABSENT = "absent"

# The fields every attestation record must carry as non-empty strings. ``view_sha``
# and ``evidence`` are optional and validated separately.
_REQUIRED_STR_FIELDS = ("attestor", "subject_kind", "subject_id", "content_sha")


@dataclass(frozen=True)
class Attestation:
    """A validated attestation — the small object every trusted record projects to.

    ``subject_kind`` / ``subject_id`` are OPAQUE to core (identity, never
    vocabulary): the kernel hashes, compares, and counts them, but never
    interprets what a subject means. ``evidence`` is likewise opaque — a payload
    the instance owns, never read here.
    """

    attestor: str
    subject_kind: str
    subject_id: str
    content_sha: str
    view_sha: str | None = None
    evidence: Any = None


def validate(record: Mapping[str, Any]) -> Attestation:
    """Validate an attestation record dict → :class:`Attestation`, or refuse.

    Enforces the record shape ONLY (the load-bearing invariants every instance
    shares): the ``attestor`` literal, the required non-empty string fields, and
    the optional ``view_sha`` shape. It never checks a subject against a
    vocabulary, and it never invents a ``subject_id`` — an empty or non-string
    id is refused, because a caller-authored id is the anti-fabrication contract
    (the same posture ``state.scopes`` keeps for tags, minus the slug pattern —
    a ``subject_id`` rides an existing record, it is not a path segment).

    Raises :class:`errors.SpecInvalid` naming the offending field.
    """
    if not isinstance(record, Mapping):
        raise errors.SpecInvalid(
            f"attestation: record must be a mapping; got {type(record).__name__}"
        )
    for field in _REQUIRED_STR_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value:
            raise errors.SpecInvalid(
                f"attestation: {field!r} must be a non-empty string; got {value!r}"
            )
    attestor = record["attestor"]
    if attestor not in ATTESTORS:
        raise errors.SpecInvalid(
            f"attestation: attestor must be one of {sorted(ATTESTORS)}; got {attestor!r}"
        )
    view_sha = record.get("view_sha")
    if view_sha is not None and (not isinstance(view_sha, str) or not view_sha):
        raise errors.SpecInvalid(
            f"attestation: view_sha, when present, must be a non-empty string; got {view_sha!r}"
        )
    return Attestation(
        attestor=attestor,
        subject_kind=record["subject_kind"],
        subject_id=record["subject_id"],
        content_sha=record["content_sha"],
        view_sha=view_sha,
        evidence=record.get("evidence"),
    )


def bind(record: Mapping[str, Any], *, recompute: Callable[[], str] | str) -> Attestation:
    """Validate *record*, then recompute-and-compare its ``content_sha`` — the lock.

    The un-fakeable lock extracted once (``notebook-audit.md`` D5 lock 2): the
    asserted ``content_sha`` must equal a freshly recomputed sha of the current
    content, so a hash cannot be asserted into existence. *recompute* is either
    the current sha string or a zero-arg callable that returns it (the callable
    form lets a caller defer the recompute until after the cheaper shape check
    passes).

    Every instance that appends an attestation binds through here — a human
    sign-off recomputes its section sha exactly as a code receipt recomputes its
    output sha. The ADDITIONAL human-authorship lock is not this function's job:
    it stays per-instance in ``ops/decision/journal.py``.

    Raises :class:`errors.SpecInvalid` on a bad record shape, a recompute that
    yields no sha, or a content_sha that does not match the recomputed one.
    """
    attestation = validate(record)
    current = recompute() if callable(recompute) else recompute
    if not isinstance(current, str) or not current:
        raise errors.SpecInvalid(
            f"attestation.bind: recompute must yield a non-empty content sha; got {current!r}"
        )
    if attestation.content_sha != current:
        raise errors.SpecInvalid(
            f"attestation.bind: asserted content_sha {attestation.content_sha!r} for subject "
            f"{attestation.subject_kind}/{attestation.subject_id} does not match the recomputed "
            f"{current!r} — a hash cannot be asserted into existence (D5 lock 2). Re-view the "
            "current content and attest its actual sha."
        )
    return attestation


def reduce(
    records: Iterable[Mapping[str, Any]],
    *,
    current_sha: str,
    subject_id: str | None = None,
) -> str:
    """Reduce a subject's attestation records to CURRENT / STALE / ABSENT.

    Drift-revocation, defined once. *records* are in APPEND (chronological)
    order — newest last, the order ``decision_journal.read_decisions`` and
    ``scopes._read_looks`` already return — so the LAST valid record is the
    newest and wins (the newest-first precedence idiom
    ``scopes.is_scope_locked`` uses, read forward to the last match instead of
    reversed to the first).

    * :data:`ABSENT` — no valid attestation for the subject.
    * :data:`CURRENT` — the newest attestation's ``content_sha`` equals
      *current_sha* (the subject is unchanged since it was attested).
    * :data:`STALE` — the newest attestation matches an OLDER sha; an edit moved
      the content and revoked the trust, with no drift state machine (the T8
      "signed section edited afterward simply reads unsigned" property).

    When *subject_id* is given, only records for that subject are considered —
    so a caller can pass a whole mixed journal and reduce per-subject without
    re-writing the filter loop (the divergence this kernel exists to prevent).
    Malformed records are skipped (the tolerant-read idiom), never raised.
    """
    newest: Attestation | None = None
    for record in records:
        try:
            attestation = validate(record)
        except errors.SpecInvalid:
            continue
        if subject_id is not None and attestation.subject_id != subject_id:
            continue
        newest = attestation  # append order → the last match is the newest
    if newest is None:
        return ABSENT
    return CURRENT if newest.content_sha == current_sha else STALE
