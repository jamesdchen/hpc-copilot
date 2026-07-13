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
* the record blocks (:data:`REGISTRATION_BLOCK` / :data:`REVOKE_BLOCK` /
  :data:`REGISTRATION_REVIEW_BLOCK`), the maintained block FAMILY
  (:data:`REGISTRATION_BLOCK_FAMILY`, R6 — the reg/revoke pair PLUS the
  ``registration-review`` re-affirmation and the ``conformance-verdict`` drift
  verdict, each a reviewed family admission), and :data:`SUBJECT_KIND`
  (``"dossier"``).
* :func:`parse_conformance_declaration` — structure-only validation of the
  OPTIONAL ``conformance`` declaration block on a registration's ``resolved``
  (C-declare), routed through the ONE validator in ``state/conformance.py``.

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
from datetime import datetime
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state import attestation, scopes

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from hpc_agent.state.conformance import ConformanceDeclaration

__all__ = [
    "PREREQUISITE_KINDS",
    "KIND_NOTEBOOK_AUDIT",
    "KIND_REPRODUCTION",
    "KIND_SCOPE_BUDGET",
    "KIND_PACK_RECEIPT",
    "KIND_ATTESTATION",
    "UNCONTESTED_REQUIRES_KEY",
    "REGISTRATION_BLOCK",
    "REVOKE_BLOCK",
    "REGISTRATION_REVIEW_BLOCK",
    "CONFORMANCE_VERDICT_BLOCK",
    "REGISTRATION_BLOCK_FAMILY",
    "SUBJECT_KIND",
    "CURRENT",
    "STALE",
    "REVOKED",
    "SUPERSEDED",
    "ABSENT",
    "STATUSES",
    "HORIZON_LAPSED",
    "PrerequisiteSpec",
    "RegistrationTemplate",
    "ChainEntry",
    "RegistrationStatus",
    "load_template",
    "parse_template",
    "parse_chain_entry",
    "parse_conformance_declaration",
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

#: R3 — the named pack slot's receipt reduces CURRENT with ``passed=true`` under
#: the pack's current bind + on-disk bytes (``state/pack_receipts.py::
#: slot_status``, the ONE reduction the submit gate also uses). The chain entry's
#: ``subject_id`` is the full address ``"<pack>:<slot>"``.
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

#: The kinds that accept NO KIND-SPECIFIC ``requires`` mapping — the generic
#: ``attestation`` escape hatch carries no evidence-tier vocabulary core could
#: interpret (R3), so a kind-specific ``requires`` on it is a loud refusal, never a
#: silent pass.
_KINDS_WITHOUT_REQUIRES = frozenset({KIND_ATTESTATION})

#: The one CROSS-KIND ``requires`` key EVERY ``PREREQUISITE_KINDS`` member accepts,
#: INCLUDING the otherwise requires-free ``attestation`` kind (challenge-attestation
#: C-registration). This is a deliberate AMENDMENT to R3's "attestation accepts NO
#: requires" line: that line's whole test was "nothing core could interpret", and
#: ``uncontested`` is a MECHANISM property core CAN check by COUNTING standing
#: challenges (``standing_challenges(content_sha=<entry sha>)`` open-count == 0 —
#: the ``evidence_meets`` declarative pattern: the caller opts in, core counts, core
#: never decides). It NEVER blocks unless the caller declares it; the per-kind check
#: lives in ``ops/registration/prereqs.py`` (T8).
UNCONTESTED_REQUIRES_KEY = "uncontested"

# --- the record blocks + the maintained block family (R6) -------------------

#: The registration block. ``append-decision`` under this block is the ONLY
#: write path (R1/R6 lock 1 — no verb, no chain, no next_block, no skill). The
#: T7 gate refuses it for any ``scope_kind`` other than ``"registration"``.
REGISTRATION_BLOCK = "registration"

#: The explicit-overturn block (R7): a human, non-bare, mandatory-reason record
#: that withdraws a registration. Binds nothing new (it recomputes no sha); the
#: reduction maps a newest-record revoke to :data:`REVOKED`.
REVOKE_BLOCK = "registration-revoke"

#: The re-affirmation block (C-horizon): a human, R6-form record that EXTENDS a
#: registration's ``review_horizon`` WITHOUT re-registration when nothing has
#: drifted. ``resolved = {registration_id, dossier_sha, review_horizon}``. It
#: binds no NEW dossier (it re-affirms the existing one — the gate's T7 leg
#: recomputes the live signature so a DRIFTED registration cannot be re-affirmed);
#: the reduction only READS its horizon (it is never a winner nor a supersession).
REGISTRATION_REVIEW_BLOCK = "registration-review"

#: The conformance drift-verdict block (live-conformance C-verdict): a human,
#: non-bare, sha-citing record resolving a ``needs_verdict`` / ``nonconforming``
#: FINDING on a registration's live evidence. ``resolved = {registration_id,
#: cites: [<receipt content_sha>, ...], note}``; it binds no NEW dossier (the
#: drift verdict is DATED EVIDENCE, never a re-registration), and the reduction
#: never treats it as a winner nor a supersession — it rides the registration's
#: journal as an ABOUT-this-registration record (the R9 scope test). The T7 gate
#: resolves each cited sha against the conformance ledger at append.
CONFORMANCE_VERDICT_BLOCK = "conformance-verdict"

#: The MAINTAINED block family the ``"registration"`` scope accepts (R6): a set
#: growing by REVIEWED addition. Ships the two registration/revoke blocks PLUS
#: ``registration-review`` (C-horizon's re-affirmation) and ``conformance-verdict``
#: (live-conformance C-verdict's drift verdict) — each admission is the reviewed
#: vocabulary change the family exists to gate. The reduction treats ONLY
#: ``registration`` / ``registration-revoke`` as winner/supersession candidates;
#: review + conformance-verdict records ride the journal without moving the status.
REGISTRATION_BLOCK_FAMILY = frozenset(
    {REGISTRATION_BLOCK, REVOKE_BLOCK, REGISTRATION_REVIEW_BLOCK, CONFORMANCE_VERDICT_BLOCK}
)

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

#: The named cause a :data:`STALE` status carries when it is TIME-based rather
#: than drift-based (C-horizon): a current registration whose ``review_horizon``
#: lapsed before ``now``. Drift-based staleness carries no cause (``None``) — this
#: is the ONE named cause, so ``verify-registration`` / the deployment refusal /
#: the queue can distinguish "the dossier moved" from "a human owes a re-affirm".
HORIZON_LAPSED = "horizon-lapsed"


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
    if kind in _KINDS_WITHOUT_REQUIRES:
        # AMENDED (C-registration): the attestation kind accepts NO kind-specific
        # requires, but DOES accept the one cross-kind ``uncontested`` demand core
        # can check by counting standing challenges. Any OTHER key is still refused.
        extra = {k for k in requires if k != UNCONTESTED_REQUIRES_KEY}
        if extra:
            raise errors.SpecInvalid(
                f"registration: {where} kind {kind!r} accepts no 'requires' other than the "
                f"cross-kind {UNCONTESTED_REQUIRES_KEY!r} (the generic attestation kind carries "
                f"no evidence-tier vocabulary core can interpret); got extra key(s) "
                f"{sorted(extra)}"
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
    * ``stale_cause`` — when ``status`` is :data:`STALE`, the named cause:
      :data:`HORIZON_LAPSED` for a lapsed ``review_horizon`` (C-horizon), or
      ``None`` for drift-based staleness (a moved/unrecomputable dossier). Always
      ``None`` for a non-stale status. Additive with a safe default — existing
      callers and the ``now=None`` path leave it ``None``, byte-identical.
    """

    registration_id: str
    status: str
    winner: Mapping[str, Any] | None
    registered_at: str | None
    superseded: tuple[Mapping[str, Any], ...] = ()
    stale_cause: str | None = None


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
    now: str | None = None,
) -> RegistrationStatus:
    """Reduce a registration_id's records to a :class:`RegistrationStatus` (R7).

    PURE over an in-memory *records* list in APPEND (chronological) order —
    newest last, the order ``decision_journal.read_decisions`` returns (T6/T5
    read the ``.hpc/registrations/<id>.decisions.jsonl`` journal and pass the
    records in; this module never touches the file). *live_dossier_sha* is the
    dossier's signature RECOMPUTED from the live stores at read time (T5 wires
    it via T3's ``compute_dossier_signature``); ``None`` when it cannot be
    recomputed (a moved/absent run) → the winner reads :data:`STALE`.

    *now* is an OPTIONAL caller ISO timestamp (the ``doctor`` deterministic-testing
    precedent): ``None`` (the safe default) runs NO horizon evaluation, so every
    existing caller is byte-identical. When supplied, C-horizon's time-based
    staleness joins drift in this ONE reduction (below).

    The reduction adds ONLY winner-selection to the ONE kernel (the
    ``_newest_valid`` precedent). The drift verdict — is the winning
    registration's recorded ``dossier_sha`` still the live one? — routes through
    :func:`~hpc_agent.state.attestation.reduce`, NEVER a re-inlined newest-first
    or sha-compare (the enforcement-map "one kernel" row):

    * :data:`ABSENT` — no record in :data:`REGISTRATION_BLOCK_FAMILY` for the id.
    * :data:`REVOKED` — the NEWEST winner record is a :data:`REVOKE_BLOCK` (an
      explicit overturn withdraws; it binds no sha). A revoke ignores the horizon.
    * :data:`CURRENT` — the newest record is a registration whose ``dossier_sha``
      equals *live_dossier_sha* (kernel :data:`~hpc_agent.state.attestation.CURRENT`)
      AND, when *now* is given, whose effective ``review_horizon`` has not lapsed.
    * :data:`STALE` — the newest registration matches an OLDER dossier sha, or the
      live sha could not be recomputed (drift; ``stale_cause`` ``None``); OR — with
      *now* — an otherwise-current registration whose effective ``review_horizon``
      is non-null and strictly before *now* (``stale_cause`` :data:`HORIZON_LAPSED`).

    A :data:`REGISTRATION_REVIEW_BLOCK` record re-affirms without re-registration
    (C-horizon): it is NEVER a winner and NEVER a supersession — it only EXTENDS
    the horizon. The effective horizon is the newest among the winning registration
    and any SUBSEQUENT current review records (:func:`_effective_horizon`).

    Registration records older than the newest registration are :data:`SUPERSEDED`
    (re-registration is the remedy for staleness) and returned in
    :attr:`RegistrationStatus.superseded`. Malformed records are skipped.
    """
    # Winner = the newest well-formed reg/revoke record; reviews are NOT winners
    # (they only extend the horizon), tracked by position for the "subsequent" test.
    winner_record: Mapping[str, Any] | None = None
    winner_index = -1
    registration_records: list[Mapping[str, Any]] = []
    review_records: list[tuple[int, Mapping[str, Any]]] = []
    for index, record in enumerate(records):
        block = record.get("block")
        if block not in REGISTRATION_BLOCK_FAMILY:
            continue
        resolved = record.get("resolved")
        resolved = resolved if isinstance(resolved, Mapping) else {}
        if resolved.get("registration_id") != registration_id:
            continue
        if block == REGISTRATION_REVIEW_BLOCK:
            review_records.append((index, record))
            continue
        if block == CONFORMANCE_VERDICT_BLOCK:
            # A conformance drift verdict is DATED EVIDENCE riding the journal
            # (live-conformance C-verdict) — never a winner nor a supersession, and
            # it never moves the registration status. Skip it in winner selection.
            continue
        winner_record = record  # append order → the last reg/revoke is the winner
        winner_index = index
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
    stale_cause: str | None = None

    # C-horizon: time-based staleness. Only an otherwise-CURRENT registration can
    # lapse (a drift-stale or revoked one already has its verdict). now=None → no
    # horizon evaluation at all (the byte-identical existing-caller path).
    if status == CURRENT and now is not None:
        horizon = _effective_horizon(winner_resolved, review_records, after=winner_index)
        if horizon is not None and _horizon_lapsed(horizon, now):
            status = STALE
            stale_cause = HORIZON_LAPSED

    return RegistrationStatus(
        registration_id=registration_id,
        status=status,
        winner=winner_resolved,
        registered_at=registered_at,
        superseded=superseded,
        stale_cause=stale_cause,
    )


def _effective_horizon(
    winner_resolved: Mapping[str, Any],
    review_records: Sequence[tuple[int, Mapping[str, Any]]],
    *,
    after: int,
) -> str | None:
    """The ``review_horizon`` in force (C-horizon), or ``None``.

    The newest horizon among the winning registration's ``conformance.review_horizon``
    and any SUBSEQUENT current review records' ``review_horizon``. A review that
    PREDATES the winning registration referred to an older registration and is
    ignored (``index <= after``); among the rest — already in append order — the
    newest (last-appended) non-null horizon wins, so a re-affirmation EXTENDS the
    horizon without re-registration. Order statistics of TIME, no cadence invented.
    """
    conformance = winner_resolved.get("conformance")
    horizon = conformance.get("review_horizon") if isinstance(conformance, Mapping) else None
    horizon = horizon if isinstance(horizon, str) and horizon else None
    for index, record in review_records:
        if index <= after:
            continue
        review_horizon = _resolved(record).get("review_horizon")
        if isinstance(review_horizon, str) and review_horizon:
            horizon = review_horizon
    return horizon


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp (a trailing ``Z`` → ``+00:00``) for comparison."""
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    return datetime.fromisoformat(raw)


def _horizon_lapsed(horizon: str, now: str) -> bool:
    """True iff review *horizon* is strictly before *now* — timestamp comparison only.

    C-horizon: core names no period and computes no cadence — it only asks whether
    a caller-computed timestamp is in the past. An unparseable or tz-mismatched
    pair yields ``False`` (no fabricated lapse — the tolerant-read idiom; the T7
    gate validates a review's horizon at append time).
    """
    try:
        return _parse_iso(horizon) < _parse_iso(now)
    except (ValueError, TypeError):
        return False


def parse_conformance_declaration(
    resolved: Mapping[str, Any],
) -> ConformanceDeclaration | None:
    """Structure-only validation of the OPTIONAL ``conformance`` declaration block.

    Conformance is opt-in per registration (the D7 fail-safe posture): an ABSENT
    block → ``None`` (no machinery runs, byte-identical). When present, routes
    through the ONE declaration validator,
    ``state/conformance.py::validate_declaration`` — never a second validator;
    unknown keys are refused LOUD there (the R4 dangling-reference posture). NO
    dossier-membership check here: the ``(path, sha256)``-in-manifest recompute is
    the append gate's T7 leg (C-declare "the append gate verifies"); the state
    substrate imports no ``ops`` and re-gathers no dossier.

    The import is FUNCTION-LOCAL: ``state/conformance.py`` imports this module at
    load (for the ONE status vocabulary), so the reverse edge stays lazy to break
    the cycle.
    """
    block = resolved.get("conformance")
    if block is None:
        return None
    from hpc_agent.state import conformance

    return conformance.validate_declaration(block)


def _resolved(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """The record's ``resolved`` mapping, or ``{}`` when absent/malformed."""
    resolved = record.get("resolved")
    return resolved if isinstance(resolved, Mapping) else {}
