"""The registration kernel — vocabulary, models, and the status reduction.

Design origin: ``docs/design/registration-kernel.md`` (R3, R5, R7, R9 and the
Wave-A T1 task). A *registration* is the last-mile deployment-boundary
attestation: it is the SAME object as every other trusted record in the system
(``state/attestation.py`` module docstring — "every trusted thing in the system
is one of these and nothing else"), riding ``append-decision`` under a gated
block, at the strongest human tier over the strongest subject it seals (the
sealed dossier, bound by its ``bundle_sha256``).

This module ships only the SUBSTRATE T4/T5/T6/T7 build on (R-numbers cite the
plan):

* :data:`PREREQUISITE_KINDS` — the CLOSED set of core MECHANISM nouns a
  prerequisite chain entry may name (R3). Equality-pinned in tests (the
  ``DOSSIER_SOURCES`` pattern) — adding a kind is a reviewed vocabulary change.
* :func:`load_template` — the caller-authored template loader (R5), validated
  STRUCTURE-only: a list of field slugs is a list of slugs, a prerequisite is a
  ``{slot, kind, requires?}`` shape. Core never interprets a slug's MEANING and
  ships NO default template (the fabrication class). The ``requires`` keys are
  NOT interpreted here — per-kind checking is T4's job (``ops/registration/
  prereqs.py``) — but the STRUCTURE (a dict) is, and the generic ``attestation``
  kind is refused any ``requires`` (nothing core could interpret).
* :func:`parse_chain_entry` / :class:`ChainEntry` — R3's naming shape
  (``{slot, kind, subject_id, content_sha, requires}``): FULL ADDRESSES, never
  bare slugs, so the gate (T7) and the per-kind composer (T4) can dispatch each
  entry to the ONE existing checker for its kind and compare shas.
* :func:`reduce_registration` / :class:`RegistrationStatus` — the append-only
  status reduction (R7): ``current | stale | revoked | superseded | absent``.
  PURE over an in-memory record list. It adds ONLY winner-selection (the
  ``state/notebook_audit.py::_newest_valid`` precedent) on top of the ONE
  kernel — the drift verdict routes through
  :func:`~hpc_agent.state.attestation.reduce`, NEVER a re-inlined newest-first
  or sha-compare (the enforcement-map "one kernel" row,
  ``docs/internals/engineering-principles.md``; pinned by an
  ``inspect.getsource`` assertion in ``tests/state/test_registration.py``).
* the record blocks (:data:`REGISTRATION_BLOCK` / :data:`REVOKE_BLOCK`), the
  maintained block FAMILY (:data:`REGISTRATION_BLOCK_FAMILY`, R6 — starting with
  those two; ``registration-review`` / ``conformance-verdict`` are PLANNED
  future members and are deliberately NOT added here), and
  :data:`SUBJECT_KIND` (``"dossier"``).

NOT in T1 (do not add here):

* ``check_chain`` — the per-kind currency dispatch (R3 table) is T4's, in
  ``ops/registration/prereqs.py``. This module ships the kinds/models/reduction
  only; the reduction exposes the winner's ``resolved`` + timestamp so T5's
  ``verify-registration`` op and T4's composer can read the run_id, dossier_sha,
  template, and chain off the winning record.
* the ``"registration"`` decision-journal scope kind + path branch — that is T6
  (``state/decision_journal.py``); this module never reaches the journal I/O.
* the ``_assert_registration_authorship`` gate (R6's three locks) — that is T7
  (``ops/decision/journal.py``).

Pure, dependency-light: this module reads no journal file and holds no SSH /
``_wire`` / scheduler import. It routes through ``state/attestation.py`` and
reuses ``state/scopes.py``'s one filesystem-safe slug class; nothing else.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state import attestation, scopes

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "PREREQUISITE_KINDS",
    "KIND_NOTEBOOK_AUDIT",
    "KIND_REPRODUCTION",
    "KIND_SCOPE_BUDGET",
    "KIND_PACK_RECEIPT",
    "KIND_ATTESTATION",
    "REGISTRATION_BLOCK",
    "REVOKE_BLOCK",
    "REGISTRATION_BLOCK_FAMILY",
    "SUBJECT_KIND",
    "CURRENT",
    "STALE",
    "REVOKED",
    "SUPERSEDED",
    "ABSENT",
    "STATUSES",
    "PrerequisiteSpec",
    "RegistrationTemplate",
    "ChainEntry",
    "RegistrationStatus",
    "load_template",
    "parse_template",
    "parse_chain_entry",
    "reduce_registration",
]

# --- the CLOSED prerequisite-kind vocabulary (R3) ---------------------------
# Each kind is a core MECHANISM noun (a store / one-definition checker), NEVER a
# domain word: T4 dispatches each to its ONE existing currency definition. This
# set is equality-pinned in the tests (the DOSSIER_SOURCES pattern) — adding a
# member is a reviewed vocabulary change, and a domain member (`backtest`,
# `risk-check`) is forbidden by the boundary-drift flag: such names ride the
# `pack-receipt` / `attestation` kinds as SLOT names, never as new core kinds.

#: R3 — every required section signed/auto-cleared AND the module sha matches
#: (``state/notebook_audit.py::audit_module`` + the linked-source drift check).
KIND_NOTEBOOK_AUDIT = "notebook-audit"

#: R3 — the newest reproduction receipt reads CURRENT, links into the dossier,
#: and meets the ``requires`` evidence-tier floor (the repro run's ledger).
KIND_REPRODUCTION = "reproduction"

#: R3 — the named scope's look count ``<=`` the caller's budget number AND the
#: scope is not locked (``state/scopes.py`` count/lock — COUNTING vs a number).
KIND_SCOPE_BUDGET = "scope-budget"

#: R3 — the named pack slot's receipt reduces CURRENT with ``passed=true``
#: (``state/pack_receipts.py`` — RESERVED; T4 ships it as a loud
#: not-yet-available refusal until the domain-packs substrate lands).
KIND_PACK_RECEIPT = "pack-receipt"

#: R3 — the generic escape hatch: the newest attestation for ``subject_id`` in a
#: named journal carries the entry's ``content_sha`` (``state/attestation.py::
#: reduce``). Accepts NO ``requires`` (nothing core could interpret); the T4
#: checker echoes the satisfying record's ``{block, attestor}`` into the brief.
KIND_ATTESTATION = "attestation"

#: The CLOSED set of prerequisite-chain kinds (R3). Equality-pinned in tests.
PREREQUISITE_KINDS = frozenset(
    {
        KIND_NOTEBOOK_AUDIT,
        KIND_REPRODUCTION,
        KIND_SCOPE_BUDGET,
        KIND_PACK_RECEIPT,
        KIND_ATTESTATION,
    }
)

#: The kinds that accept NO ``requires`` mapping — the generic ``attestation``
#: escape hatch carries no evidence-tier vocabulary core could interpret (R3),
#: so a non-empty ``requires`` on it is a loud refusal, never a silent pass.
_KINDS_WITHOUT_REQUIRES = frozenset({KIND_ATTESTATION})

# --- the record blocks + the maintained block family (R6) -------------------

#: The registration block. ``append-decision`` under this block is the ONLY
#: write path (R1/R6 lock 1 — no verb, no chain, no next_block, no skill). The
#: T7 gate refuses it for any ``scope_kind`` other than ``"registration"``.
REGISTRATION_BLOCK = "registration"

#: The explicit-overturn block (R7): a human, non-bare, mandatory-reason record
#: that withdraws a registration. Binds nothing new (it recomputes no sha); the
#: reduction maps a newest-record revoke to :data:`REVOKED`.
REVOKE_BLOCK = "registration-revoke"

#: The MAINTAINED block family the ``"registration"`` scope accepts (R6): a set
#: growing by REVIEWED addition. It starts with exactly the two blocks this task
#: ships; ``registration-review`` / ``conformance-verdict`` are PLANNED members
#: (``docs/design/live-conformance.md``) and are deliberately NOT added here —
#: adding one is the reviewed vocabulary change the family exists to gate.
REGISTRATION_BLOCK_FAMILY = frozenset({REGISTRATION_BLOCK, REVOKE_BLOCK})

#: The opaque attestation ``subject_kind`` every registration rides (R1). The
#: kernel never interprets it; it distinguishes this subject class (the sealed
#: dossier) from notebook sections / scope locks / receipts sharing the journal.
SUBJECT_KIND = "dossier"

# --- the status vocabulary (R7) ---------------------------------------------
# `current` requires the newest record to be a registration whose live dossier
# signature still holds; `stale` names a drifted leg; `revoked` is a newest
# overturn; `superseded` is a registration made historical by a NEWER one under
# the same id; `absent` is no registration at all. Re-registration and
# revocation are the only remedies — no permanence, ever (the drift-flag).

CURRENT = "current"
STALE = "stale"
REVOKED = "revoked"
SUPERSEDED = "superseded"
ABSENT = "absent"

#: Every status the registration reduction can yield.
STATUSES = frozenset({CURRENT, STALE, REVOKED, SUPERSEDED, ABSENT})


def _validate_slug(value: Any, *, what: str) -> str:
    """Validate a caller-authored *value* as a filesystem-safe slug, or refuse.

    Reuses ``state/scopes.py``'s ONE slug class (``validate_tag`` → the shared
    ``^[A-Za-z0-9._-]+$`` pattern ``RunIdStrict``/``CampaignId`` pin on the
    wire) — never a second slug definition. Shape is the ONLY constraint; core
    never reads a slug for meaning (the opaque-by-construction row). Re-raises
    with the *what* context so a bad field slug names itself, not "scope tag".
    """
    if not isinstance(value, str):
        raise errors.SpecInvalid(f"registration: {what} must be a string; got {value!r}")
    try:
        scopes.validate_tag(value)
    except errors.SpecInvalid as exc:
        raise errors.SpecInvalid(f"registration: {what} — {exc}") from exc
    return value


def _validate_requires(kind: str, requires: Any, *, where: str) -> dict[str, Any]:
    """Validate a prerequisite ``requires`` payload STRUCTURE-only (R3/R5).

    * present-and-a-dict, or absent (→ ``{}``); a non-dict is a loud refusal.
    * the generic :data:`KIND_ATTESTATION` kind accepts NONE — a non-empty
      ``requires`` on it is refused (nothing core could interpret; R3).

    The KEYS inside are NOT interpreted here — per-kind ``requires`` checking is
    T4's job (``ops/registration/prereqs.py``); an unknown key for a kind is
    that layer's loud ``SpecInvalid``, not this loader's. This function pins only
    the shape and the attestation-takes-none rule.
    """
    if requires is None:
        requires = {}
    if not isinstance(requires, Mapping):
        raise errors.SpecInvalid(
            f"registration: {where} 'requires' must be a mapping when present; got {requires!r}"
        )
    if kind in _KINDS_WITHOUT_REQUIRES and requires:
        raise errors.SpecInvalid(
            f"registration: {where} kind {kind!r} accepts no 'requires' (the generic "
            f"attestation kind carries no evidence-tier vocabulary core can interpret); "
            f"got {dict(requires)!r}"
        )
    return dict(requires)


# --- the template (R5) ------------------------------------------------------


@dataclass(frozen=True)
class PrerequisiteSpec:
    """One TEMPLATE prerequisite declaration (R5): ``{slot, kind, requires?}``.

    A template names WHAT a registration must satisfy; a :class:`ChainEntry`
    (R3) supplies the concrete ADDRESS (``subject_id`` + ``content_sha``) that
    fills the slot at registration time. ``slot`` is a caller-authored slug;
    ``kind`` is one of :data:`PREREQUISITE_KINDS`; ``requires`` is opaque
    per-kind demand vocabulary (T4 interprets it, never core).
    """

    slot: str
    kind: str
    requires: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RegistrationTemplate:
    """A caller-authored registration template, validated STRUCTURE-only (R5).

    * ``fields`` — declared field slugs (opaque caller data; completeness is
      COUNTING against a registration's ``resolved["fields"]``, never a value
      read for meaning).
    * ``prerequisites`` — the declared prerequisite slots (:class:`PrerequisiteSpec`).
    * ``template_sha`` — the RAW-BYTES sha of the source file (R5: a template is
      bind-as-data, not percent-format Python, so ``normalize_source`` does NOT
      apply — one file, one canonical form). Recorded on every registration; a
      later template edit moves this sha and ``verify-registration`` REPORTS the
      drift as ``template: stale`` (never a silent revoke — R5's recorded
      divergence from the pack-receipt posture).
    """

    fields: tuple[str, ...]
    prerequisites: tuple[PrerequisiteSpec, ...]
    template_sha: str


def parse_template(raw: Mapping[str, Any], *, template_sha: str) -> RegistrationTemplate:
    """Validate a template mapping → :class:`RegistrationTemplate`, or refuse.

    STRUCTURE-only (R5): ``fields`` is a non-empty list of distinct field slugs
    (shared slug class); ``prerequisites`` is a list of ``{slot, kind,
    requires?}`` shapes (slot a slug, kind in :data:`PREREQUISITE_KINDS`,
    ``requires`` a dict — attestation-takes-none). A slug is never read for
    meaning; an unknown ``requires`` KEY is not this loader's concern (T4's).

    *template_sha* is the raw-bytes sha supplied by :func:`load_template`; this
    split keeps :func:`parse_template` pure over an in-memory mapping (testable
    without a file) while the sha stays a raw-bytes fact of the source.

    Raises :class:`errors.SpecInvalid` naming the offending element.
    """
    if not isinstance(raw, Mapping):
        raise errors.SpecInvalid(
            f"registration template: must be a mapping; got {type(raw).__name__}"
        )
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise errors.SpecInvalid(
            f"registration template: 'fields' must be a non-empty list of field slugs; "
            f"got {raw_fields!r}"
        )
    fields: list[str] = []
    seen: set[str] = set()
    for entry in raw_fields:
        slug = _validate_slug(entry, what="template field slug")
        if slug in seen:
            raise errors.SpecInvalid(
                f"registration template: duplicate field slug {slug!r} (each declared "
                "field is counted once; a repeat is a template authoring error)"
            )
        seen.add(slug)
        fields.append(slug)

    raw_prereqs = raw.get("prerequisites", [])
    if not isinstance(raw_prereqs, list):
        raise errors.SpecInvalid(
            f"registration template: 'prerequisites' must be a list when present; "
            f"got {raw_prereqs!r}"
        )
    prerequisites: list[PrerequisiteSpec] = []
    slots_seen: set[str] = set()
    for i, entry in enumerate(raw_prereqs):
        where = f"prerequisite[{i}]"
        if not isinstance(entry, Mapping):
            raise errors.SpecInvalid(
                f"registration template: {where} must be a mapping; got {entry!r}"
            )
        slot = _validate_slug(entry.get("slot"), what=f"{where} slot")
        if slot in slots_seen:
            raise errors.SpecInvalid(
                f"registration template: {where} duplicate slot {slot!r} (each prerequisite "
                "slot is filled once)"
            )
        slots_seen.add(slot)
        kind = entry.get("kind")
        if kind not in PREREQUISITE_KINDS:
            raise errors.SpecInvalid(
                f"registration template: {where} kind {kind!r} is not one of the closed "
                f"PREREQUISITE_KINDS {sorted(PREREQUISITE_KINDS)}"
            )
        requires = _validate_requires(kind, entry.get("requires"), where=where)
        prerequisites.append(PrerequisiteSpec(slot=slot, kind=kind, requires=requires))

    return RegistrationTemplate(
        fields=tuple(fields),
        prerequisites=tuple(prerequisites),
        template_sha=template_sha,
    )


def load_template(path: Path) -> RegistrationTemplate:
    """Load + validate a caller-referenced template JSON file (R5).

    Reads the file's RAW BYTES, computes ``template_sha`` over them (not
    ``normalize_source`` — a template is bind-as-data, not percent-format
    Python), parses the JSON, and validates it via :func:`parse_template`. Core
    ships NO default template: a missing or unreadable *path* is a loud refusal,
    never a silent pass (the fabrication class).

    Raises :class:`errors.SpecInvalid` on a missing file, non-JSON content, or a
    structurally invalid template.
    """
    import json

    try:
        data = path.read_bytes()
    except (OSError, FileNotFoundError) as exc:
        raise errors.SpecInvalid(
            f"registration template: cannot read template file {str(path)!r} ({exc}); "
            "core ships no default template — a registration must reference a real file"
        ) from exc
    template_sha = hashlib.sha256(data).hexdigest()
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"registration template: {str(path)!r} is not valid UTF-8 JSON ({exc})"
        ) from exc
    return parse_template(raw, template_sha=template_sha)


# --- the chain entry (R3) ---------------------------------------------------


@dataclass(frozen=True)
class ChainEntry:
    """One prerequisite-chain entry (R3): a FULL ADDRESS, never a bare slug.

    ``{slot, kind, subject_id, content_sha, requires}``. A bare slug was
    rejected because it cannot be mechanically checked for currency; the full
    address lets the T4 composer dispatch each entry to the ONE existing checker
    for its ``kind`` and compare the asserted ``content_sha`` against the
    checker's recomputed answer — the chain is a list of recompute LOCKS, not a
    list of claims.

    * ``slot`` — the caller-authored slug matching a template prerequisite.
    * ``kind`` — one of :data:`PREREQUISITE_KINDS`.
    * ``subject_id`` — OPAQUE: an ``audit_id`` / ``run_id`` / scope tag / pack
      slot, identity-compared by the checker, never read for meaning.
    * ``content_sha`` — the sha the prerequisite was CURRENT at (R3's per-kind
      recompute leg re-derives it at append/verify time).
    * ``requires`` — opaque per-kind demand vocabulary (T4 interprets; the
      generic ``attestation`` kind carries none).
    """

    slot: str
    kind: str
    subject_id: str
    content_sha: str
    requires: Mapping[str, Any] = field(default_factory=dict)


def parse_chain_entry(raw: Mapping[str, Any]) -> ChainEntry:
    """Validate a chain-entry mapping → :class:`ChainEntry`, or refuse (R3).

    ``slot`` is a slug (shared class); ``kind`` must be in
    :data:`PREREQUISITE_KINDS` (an unknown kind is a loud
    :class:`errors.SpecInvalid`); ``subject_id`` / ``content_sha`` are non-empty
    opaque strings (a full address, never a bare slug); ``requires`` is a dict —
    and the generic :data:`KIND_ATTESTATION` kind is refused any non-empty one.
    The ``requires`` KEYS are not interpreted here (T4's per-kind job).

    Raises :class:`errors.SpecInvalid` naming the offending field.
    """
    if not isinstance(raw, Mapping):
        raise errors.SpecInvalid(f"registration chain entry: must be a mapping; got {raw!r}")
    slot = _validate_slug(raw.get("slot"), what="chain entry slot")
    kind = raw.get("kind")
    if kind not in PREREQUISITE_KINDS:
        raise errors.SpecInvalid(
            f"registration chain entry {slot!r}: kind {kind!r} is not one of the closed "
            f"PREREQUISITE_KINDS {sorted(PREREQUISITE_KINDS)}"
        )
    subject_id = raw.get("subject_id")
    if not isinstance(subject_id, str) or not subject_id:
        raise errors.SpecInvalid(
            f"registration chain entry {slot!r}: 'subject_id' must be a non-empty opaque "
            f"string (the prerequisite's full address); got {subject_id!r}"
        )
    content_sha = raw.get("content_sha")
    if not isinstance(content_sha, str) or not content_sha:
        raise errors.SpecInvalid(
            f"registration chain entry {slot!r}: 'content_sha' must be a non-empty string "
            f"(the sha the prerequisite was current at); got {content_sha!r}"
        )
    requires = _validate_requires(kind, raw.get("requires"), where=f"chain entry {slot!r}")
    return ChainEntry(
        slot=slot,
        kind=kind,
        subject_id=subject_id,
        content_sha=content_sha,
        requires=requires,
    )


# --- the status reduction (R7) ----------------------------------------------


@dataclass(frozen=True)
class RegistrationStatus:
    """The reduced status of one ``registration_id`` (R7/R8 shape).

    * ``registration_id`` — the caller-authored id these records concern.
    * ``status`` — the id's overall status: :data:`CURRENT` / :data:`STALE` /
      :data:`REVOKED` / :data:`ABSENT`. It is NEVER :data:`SUPERSEDED` — that is
      a per-record label carried on the entries of :attr:`superseded` (an
      individual older registration is superseded; the id as a whole is
      described by its winner).
    * ``winner`` — the winning (newest) record's ``resolved`` mapping, or
      ``None`` when :data:`ABSENT`. For a revoke winner this is the revoke
      record's ``resolved``; otherwise the newest registration's — the object
      T5's ``verify-registration`` reads ``run_id`` / ``dossier_sha`` /
      ``template`` / ``prerequisites`` off, and T4 reads the chain off.
    * ``registered_at`` — the winning record's journal timestamp (``ts``), or
      ``None`` — R8's ``registered_at`` leg.
    * ``superseded`` — the ``resolved`` mappings of every registration record
      made historical by a newer registration, newest-superseded last. Each is
      a :data:`SUPERSEDED` record; the attention queue / history views read it.
    """

    registration_id: str
    status: str
    winner: Mapping[str, Any] | None
    registered_at: str | None
    superseded: tuple[Mapping[str, Any], ...] = ()


def _project_registration(record: Mapping[str, Any], registration_id: str) -> dict[str, Any] | None:
    """Project a REGISTRATION journal record to an attestation dict, or ``None``.

    Returns ``None`` for any record that is not a :data:`REGISTRATION_BLOCK`
    record for *registration_id* (revoke records bind no new sha and are handled
    by winner-selection directly, never routed through the kernel). A recognised
    block with a malformed ``resolved`` still projects; the kernel's
    :func:`~hpc_agent.state.attestation.validate`/``reduce`` then skips it — one
    bad line never strands the trail (the tolerant-read idiom).
    """
    if record.get("block") != REGISTRATION_BLOCK:
        return None
    resolved = record.get("resolved")
    resolved = resolved if isinstance(resolved, Mapping) else {}
    if resolved.get("registration_id") != registration_id:
        return None
    projected: dict[str, Any] = {
        "attestor": "human",
        "subject_kind": SUBJECT_KIND,
        "subject_id": registration_id,
        "content_sha": resolved.get("dossier_sha"),
    }
    view_sha = resolved.get("view_sha")
    if view_sha:
        projected["view_sha"] = view_sha
    return projected


def reduce_registration(
    records: Sequence[Mapping[str, Any]],
    *,
    registration_id: str,
    live_dossier_sha: str | None,
) -> RegistrationStatus:
    """Reduce a registration_id's records to a :class:`RegistrationStatus` (R7).

    PURE over an in-memory *records* list in APPEND (chronological) order —
    newest last, the order ``decision_journal.read_decisions`` returns (T6/T5
    read the ``.hpc/registrations/<id>.decisions.jsonl`` journal and pass the
    records in; this module never touches the file). *live_dossier_sha* is the
    dossier's signature RECOMPUTED from the live stores at read time (T5 wires
    it via T3's ``compute_dossier_signature``); ``None`` when it cannot be
    recomputed (a moved/absent run) → the winner reads :data:`STALE`.

    The reduction adds ONLY winner-selection to the ONE kernel (the
    ``_newest_valid`` precedent). The drift verdict — is the winning
    registration's recorded ``dossier_sha`` still the live one? — routes through
    :func:`~hpc_agent.state.attestation.reduce`, NEVER a re-inlined newest-first
    or sha-compare (the enforcement-map "one kernel" row):

    * :data:`ABSENT` — no record in :data:`REGISTRATION_BLOCK_FAMILY` for the id.
    * :data:`REVOKED` — the NEWEST family record is a :data:`REVOKE_BLOCK` (an
      explicit overturn withdraws; it binds no sha).
    * :data:`CURRENT` — the newest record is a registration whose ``dossier_sha``
      equals *live_dossier_sha* (kernel :data:`~hpc_agent.state.attestation.CURRENT`).
    * :data:`STALE` — the newest registration matches an OLDER dossier sha, or
      the live sha could not be recomputed (a sealed store moved out from under
      the registration — failure class 4 closed for free at read time).

    Registration records older than the newest registration are :data:`SUPERSEDED`
    (re-registration is the remedy for staleness) and returned in
    :attr:`RegistrationStatus.superseded`. Malformed records are skipped.
    """
    # Winner = the newest well-formed family record (registration OR revoke).
    winner_record: Mapping[str, Any] | None = None
    registration_records: list[Mapping[str, Any]] = []
    for record in records:
        block = record.get("block")
        if block not in REGISTRATION_BLOCK_FAMILY:
            continue
        resolved = record.get("resolved")
        resolved = resolved if isinstance(resolved, Mapping) else {}
        if resolved.get("registration_id") != registration_id:
            continue
        winner_record = record  # append order → the last match is the newest
        if block == REGISTRATION_BLOCK:
            registration_records.append(record)

    if winner_record is None:
        return RegistrationStatus(
            registration_id=registration_id,
            status=ABSENT,
            winner=None,
            registered_at=None,
            superseded=(),
        )

    # Older registrations (all but the newest) are superseded, newest-last.
    superseded = tuple(_resolved(r) for r in registration_records[:-1])
    winner_resolved = _resolved(winner_record)
    registered_at = winner_record.get("ts")
    registered_at = registered_at if isinstance(registered_at, str) else None

    if winner_record.get("block") == REVOKE_BLOCK:
        return RegistrationStatus(
            registration_id=registration_id,
            status=REVOKED,
            winner=winner_resolved,
            registered_at=registered_at,
            superseded=superseded,
        )

    # The newest record is a registration: route the drift verdict through the
    # ONE kernel (never re-inline the sha-compare). A None live sha cannot match
    # any recorded sha → the kernel reads it STALE (a moved/absent dossier).
    projected = [
        p
        for p in (_project_registration(r, registration_id) for r in registration_records)
        if p is not None
    ]
    verdict = attestation.reduce(
        projected,
        current_sha=live_dossier_sha if live_dossier_sha is not None else "",
        subject_id=registration_id,
    )
    status = CURRENT if verdict == attestation.CURRENT else STALE
    return RegistrationStatus(
        registration_id=registration_id,
        status=status,
        winner=winner_resolved,
        registered_at=registered_at,
        superseded=superseded,
    )


def _resolved(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """The record's ``resolved`` mapping, or ``{}`` when absent/malformed."""
    resolved = record.get("resolved")
    return resolved if isinstance(resolved, Mapping) else {}
