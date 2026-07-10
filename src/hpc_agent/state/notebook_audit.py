"""Per-section audit-state reduction for the notebook-audit substrate (T6).

Design origin: ``docs/design/notebook-audit.md`` (Wave B / T6 + decisions D3,
D5, D-attention, and the T0 attestation-kernel section). This module answers one
question per audited section — *is this section's current source cleared for
graduation, and by whom?* — and rolls the per-section answers into the gate's
whole-module pass predicate (T9 consumes the rollup).

The journal is the source of truth. A notebook audit lives under the
``"notebook"`` decision-journal scope (D3, T7): every touchpoint for an
``audit_id`` is an append-only record in
``.hpc/notebooks/<audit_id>.decisions.jsonl``. Two record *classes* ride that
one journal, and they are the SAME attestation object (the reuse-accounting
paragraph): a HUMAN sign-off and a CODE auto-clear. They differ only in the
``attestor`` and in which additional lock applies (authorship vs recompute) —
never in the record shape or in how drift revokes them.

Record shapes (the two blocks this module reads and — for the code class —
writes):

* **Human sign-off** — ``block="notebook-sign-off"``,
  ``resolved={audit_id, section, section_sha, view_sha?}``. The ``append-decision``
  authorship gate (T8) is what makes it un-fakeable on the human side; here it
  simply projects to a ``human`` attestation.
* **Code auto-clear** — ``block="notebook-auto-clear"``,
  ``resolved={audit_id, section, section_sha, view_sha?, attestor:"code"}``,
  ``response="auto_cleared"``. THIS module owns the writer
  (:func:`record_auto_clear`).

**Auto-clear record-shape decision (recorded per the T6 brief).** The natural
mirror of the sign-off block is ``block="notebook-auto-clear"`` — a distinct
block so a reader never confuses a machine clearance with a human ack. The
``response`` field is the honest, mechanical string ``"auto_cleared"`` and NEVER
a human-ack token (no ``"y"``, no free text): a code record must never read as a
human's approval when the journal is replayed or exported. The ``attestor:"code"``
marker rides ``resolved`` so the projection can label the winner without guessing
from the block name alone, and the writer routes the record through the kernel's
:func:`~hpc_agent.state.attestation.bind` recompute-lock exactly as a human
sign-off would — a code clearance still cannot assert a section sha that does not
match the ``.py`` on disk (D5 lock 2, "CODE attestations face recompute").

**The reduction routes through the ONE kernel** (``state/attestation.py``, T0):
the drift verdict (``current`` / ``stale`` / ``absent``) is
:func:`~hpc_agent.state.attestation.reduce`'s newest-first decision, never
re-inlined here (the enforcement-map "one kernel" row —
``docs/internals/engineering-principles.md``; the route-through is pinned by an
``inspect.getsource`` assertion in ``tests/state/test_notebook_audit.py``). This
module adds only the ATTESTOR-of-the-winner projection the kernel does not
surface (the kernel reduces to a verdict, not to a winning record), which is a
selection over identity — never a second copy of the drift comparison.

Public per-section status vocabulary (T6):

* ``signed_current``  — current verdict, newest valid record is a HUMAN sign-off.
* ``auto_cleared``    — current verdict, newest valid record is a CODE auto-clear.
* ``signed_stale``    — stale verdict, newest valid record is a HUMAN sign-off
  (the section was signed, then its source moved — an informational state that
  tells the human their approval was revoked by an edit).
* ``unsigned``        — no valid record (absent), OR a STALE record whose newest
  valid attestation is a CODE auto-clear. **A stale auto-clear falls through to
  ``unsigned`` by construction** (recorded reason): drift = unsigned (the T8
  "signed section edited afterward simply reads unsigned" property); a machine
  clearance carries no human to inform, so — unlike a stale human sign-off, which
  earns the distinct ``signed_stale`` signal — it has no distinct state worth
  surfacing. Both fail the gate identically; only the label differs.

The gate's pass predicate: a section passes iff its status is ``signed_current``
or ``auto_cleared`` (both are current). :class:`ModuleAudit.passed` is that
predicate over every REQUIRED (template) section.

Pure-ish: this module reads the decision journal (I/O via
:mod:`hpc_agent.state.decision_journal`) and writes the auto-clear record, but it
holds no SSH, no ``_wire`` import, and no scheduler — the ``ops`` layer owns the
Pydantic boundary and the file-reading of the ``.py`` source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hpc_agent.state import attestation
from hpc_agent.state.decision_journal import append_decision, read_decisions

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path
    from typing import Any

    from hpc_agent.state.attestation import Attestation
    from hpc_agent.state.audit_source import ParsedModule, Section

__all__ = [
    "SIGN_OFF_BLOCK",
    "AUTO_CLEAR_BLOCK",
    "RENDER_RECEIPT_BLOCK",
    "DRAFT_BLOCK",
    "AUDIT_CONFIG_BLOCK",
    "SUBJECT_KIND",
    "DRAFT_SUBJECT_KIND",
    "AUTO_CLEAR_RESPONSE",
    "RENDER_RECEIPT_RESPONSE",
    "DRAFT_RESPONSE",
    "AUDIT_CONFIG_RESPONSE",
    "SIGNED_CURRENT",
    "AUTO_CLEARED",
    "SIGNED_STALE",
    "UNSIGNED",
    "SECTION_STATUSES",
    "PASSING_STATUSES",
    "SectionAudit",
    "ModuleAudit",
    "audit_section",
    "audit_module",
    "record_auto_clear",
    "record_render_receipt",
    "read_render_receipts",
    "record_draft",
    "read_draft_author",
    "record_audit_config",
    "read_audit_config",
    "read_audit_intent",
    "RELAY_DUE_BLOCK",
    "RELAY_DUE_RESPONSE",
    "RELAY_DUE_RECORD_KIND",
    "RENDER_RELAY_DUE_RECORD_KIND",
    "RELAY_DISCHARGE_BLOCK",
    "RELAY_DISCHARGE_RESPONSE",
    "DISCHARGED_BY_RELAY",
    "DISCHARGED_BY_COMPLETER",
    "record_relay_due",
    "record_relay_discharge",
    "read_undischarged_relay_markers",
]

#: The human sign-off block (D3). A ``notebook-sign-off`` append-decision record
#: projects to a ``human`` attestation.
SIGN_OFF_BLOCK = "notebook-sign-off"

#: The code auto-clear block — the machine mirror of the sign-off. Distinct so a
#: reader never mistakes a mechanical clearance for a human ack.
AUTO_CLEAR_BLOCK = "notebook-auto-clear"

#: The honest, mechanical ``response`` a code auto-clear carries — never a human
#: ack token.
AUTO_CLEAR_RESPONSE = "auto_cleared"

#: The code render-receipt block (T10). A third block class riding the SAME
#: notebook journal: a CODE attestation that a section's source was RENDERED
#: (executed) at a bound sha, evidence for the assertions-green leg of the
#: D-attention tier. Distinct block so a reader never confuses execution
#: evidence with a clearance (auto-clear) or a human ack (sign-off).
RENDER_RECEIPT_BLOCK = "notebook-render-receipt"

#: The honest, mechanical ``response`` a render receipt carries — the execution
#: happened, nothing was approved. NEVER a human-ack token (no ``"y"``) and never
#: a clearance token (not ``"auto_cleared"``): a receipt is evidence, not a
#: sign-off class.
RENDER_RECEIPT_RESPONSE = "rendered"

#: The DRAFT-attestation block (multi-human MH5). A block class riding the SAME
#: notebook journal: a CODE attestation that a section's source was DRAFTED by
#: the actor whose session recorded it, bound to the section sha it was drafted
#: at. Distinct block so a reader never confuses drafter-authorship with a
#: clearance (auto-clear), execution evidence (receipt), or a human ack
#: (sign-off). It is the SECTION AUTHOR the reviewer!=author gate (MH6) resolves.
DRAFT_BLOCK = "notebook-draft"

#: The honest, mechanical ``response`` a draft attestation carries — a draft was
#: recorded, nothing was approved and nothing executed. NEVER a human-ack token.
DRAFT_RESPONSE = "drafted"

#: The opaque attestation ``subject_kind`` a draft attestation rides (MH5 — a
#: DISTINCT kind from :data:`SUBJECT_KIND`, so a draft never enters the sign-off /
#: auto-clear reduction and can never green a section). The kernel never
#: interprets it.
DRAFT_SUBJECT_KIND = "notebook-draft"

#: The audit-CONFIG record block (run-#10 standalone-audit seat). A FOURTH block
#: class riding the same notebook journal: the audit configuration
#: (``input_roots`` / ``source_roots`` / ``attention_order`` / ``output_roots``)
#: recorded for a STANDALONE audit — one with no interview.json
#: ``audited_source`` opt-in, which previously ran ROOTLESS-canonical (no seat
#: held the config, so the template-mandated ``source_roots`` binding was
#: silently inactive). NOT an attestation: it carries no section, no
#: ``content_sha``, and it is deliberately absent from :data:`_BLOCK_ATTESTOR`
#: (and from :func:`_project_receipt`), so it can never enter the sign-off
#: reduction or the receipt read.
AUDIT_CONFIG_BLOCK = "notebook-audit-config"

#: The honest, mechanical ``response`` a config record carries — a configuration
#: was recorded, nothing was approved and nothing executed. Never a human-ack
#: token.
AUDIT_CONFIG_RESPONSE = "config_recorded"

#: The opaque attestation ``subject_kind`` every notebook section rides. The
#: kernel never interprets it; it distinguishes this subject class from scope
#: locks / greenlights / receipts sharing the same journal machinery.
SUBJECT_KIND = "notebook-section"

# --- the per-section status vocabulary (T6) ---------------------------------
SIGNED_CURRENT = "signed_current"
AUTO_CLEARED = "auto_cleared"
SIGNED_STALE = "signed_stale"
UNSIGNED = "unsigned"

#: Every status a section reduction can yield.
SECTION_STATUSES = frozenset({SIGNED_CURRENT, AUTO_CLEARED, SIGNED_STALE, UNSIGNED})

#: The statuses that PASS the graduation gate — both are "current at this hash"
#: (human-signed or machine-cleared). The rollup's :attr:`ModuleAudit.passed`
#: requires every required section to be one of these.
PASSING_STATUSES = frozenset({SIGNED_CURRENT, AUTO_CLEARED})

# block → the attestor that block class carries. A record whose block is not a
# notebook attestation block is not projected at all (skipped).
_BLOCK_ATTESTOR = {SIGN_OFF_BLOCK: "human", AUTO_CLEAR_BLOCK: "code"}


@dataclass(frozen=True)
class SectionAudit:
    """The reduced audit state of one section.

    * ``slug`` — the section's caller-authored id.
    * ``status`` — one of :data:`SECTION_STATUSES`.
    * ``current_section_sha`` — the section's CURRENT sha recomputed from the
      ``.py`` on disk (``None`` when a required/template section is absent from
      the source entirely — nothing to sign).
    * ``signed_section_sha`` — the ``content_sha`` of the newest valid
      attestation (the sha the human/code actually attested), or ``None`` when
      there is no valid record.
    * ``view_sha`` — the projection sha the newest valid attestation recorded
      (what the human saw), or ``None``.
    * ``attestor`` — ``"human"`` / ``"code"`` of the newest valid record, or
      ``None`` when unsigned-by-absence.
    """

    slug: str
    status: str
    current_section_sha: str | None
    signed_section_sha: str | None = None
    view_sha: str | None = None
    attestor: str | None = None


@dataclass(frozen=True)
class ModuleAudit:
    """The whole-module rollup over every REQUIRED (template) section.

    * ``sections`` — per required-slug :class:`SectionAudit`, in template order.
    * ``passed`` — the gate predicate: every required section is
      :data:`PASSING_STATUSES` (``signed_current`` or ``auto_cleared``). An empty
      required set passes vacuously (an undisciplined/absent template gates
      nothing — the D7 fail-safe posture).
    """

    sections: tuple[SectionAudit, ...]
    passed: bool


def _project(record: dict[str, Any]) -> dict[str, Any] | None:
    """Project a decision-journal record to an attestation-record dict, or ``None``.

    Returns ``None`` for any record that is not a notebook attestation (a block
    outside :data:`_BLOCK_ATTESTOR`) — those are filtered out before the kernel
    ever sees them. A recognised block with a malformed ``resolved`` still
    projects; the kernel's :func:`~hpc_agent.state.attestation.validate` then
    refuses it (missing/empty ``subject_id`` or ``content_sha``) and the reducer
    skips it — one bad line never strands the rest of the audit trail.
    """
    block = record.get("block")
    attestor = _BLOCK_ATTESTOR.get(block) if isinstance(block, str) else None
    if attestor is None:
        return None
    resolved = record.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    projected: dict[str, Any] = {
        "attestor": attestor,
        "subject_kind": SUBJECT_KIND,
        "subject_id": resolved.get("section"),
        "content_sha": resolved.get("section_sha"),
    }
    view_sha = resolved.get("view_sha")
    if view_sha:
        projected["view_sha"] = view_sha
    return projected


def _projected_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every notebook-attestation projection in *records*, in append order."""
    out: list[dict[str, Any]] = []
    for record in records:
        projected = _project(record)
        if projected is not None:
            out.append(projected)
    return out


def _newest_valid(projected: Sequence[dict[str, Any]], slug: str) -> Attestation | None:
    """Return the newest VALID attestation for *slug*, or ``None``.

    Selection only — the ``current``/``stale``/``absent`` DRIFT decision is
    :func:`~hpc_agent.state.attestation.reduce`'s job and is NOT reproduced here
    (this never compares a ``content_sha`` to the current sha). This reads the
    attestor + attested shas of the winning record, which the kernel's verdict
    does not surface. Append order → the last valid match is the newest (the
    kernel's own precedence). Malformed records are skipped, never raised.
    """
    from hpc_agent import errors

    newest: Attestation | None = None
    for record in projected:
        try:
            att = attestation.validate(record)
        except errors.SpecInvalid:
            continue
        if att.subject_id != slug:
            continue
        newest = att
    return newest


def audit_section(
    records: Sequence[dict[str, Any]],
    slug: str,
    current_section_sha: str | None,
) -> SectionAudit:
    """Reduce one section's journal records to a :class:`SectionAudit`.

    *records* are the whole notebook journal (append order, newest last — the
    order :func:`~hpc_agent.state.decision_journal.read_decisions` returns); this
    filters to *slug*'s notebook attestations. *current_section_sha* is the
    section's sha recomputed from the ``.py`` on disk (``None`` when the section
    is absent from the source — a required template section with no body to
    sign, which can never be current → ``unsigned``).

    The drift verdict is the kernel's; the attestor of the winning record maps
    the verdict onto the T6 vocabulary:

    * ``current``  + human → ``signed_current`` ; + code → ``auto_cleared``
    * ``stale``    + human → ``signed_stale``   ; + code → ``unsigned``
      (a stale auto-clear falls through to ``unsigned`` by construction)
    * ``absent``           → ``unsigned``
    """
    projected = _projected_records(records)
    newest = _newest_valid(projected, slug)
    if current_section_sha is None or newest is None:
        # No current content to match, or no valid attestation → unsigned. Still
        # surface the newest record's identity when one exists (a signed section
        # missing from the current source).
        return SectionAudit(
            slug=slug,
            status=UNSIGNED,
            current_section_sha=current_section_sha,
            signed_section_sha=newest.content_sha if newest else None,
            view_sha=newest.view_sha if newest else None,
            attestor=newest.attestor if newest else None,
        )

    # Route the drift decision through the ONE kernel (never re-inlined here).
    verdict = attestation.reduce(projected, current_sha=current_section_sha, subject_id=slug)
    if verdict == attestation.CURRENT:
        status = SIGNED_CURRENT if newest.attestor == "human" else AUTO_CLEARED
    elif verdict == attestation.STALE:
        # A stale HUMAN sign-off earns the informational signed_stale; a stale
        # CODE auto-clear has no human to inform and falls through to unsigned
        # (drift = unsigned by construction).
        status = SIGNED_STALE if newest.attestor == "human" else UNSIGNED
    else:  # attestation.ABSENT — unreachable (newest is not None here), defensive.
        status = UNSIGNED
    return SectionAudit(
        slug=slug,
        status=status,
        current_section_sha=current_section_sha,
        signed_section_sha=newest.content_sha,
        view_sha=newest.view_sha,
        attestor=newest.attestor,
    )


def audit_module(
    experiment_dir: Path,
    audit_id: str,
    *,
    source: ParsedModule,
    required_slugs: Sequence[str],
) -> ModuleAudit:
    """Reduce every REQUIRED section to the whole-module rollup.

    Reads *audit_id*'s notebook journal once, indexes *source*'s parsed sections
    by slug, and reduces each *required_slugs* entry against its CURRENT source
    sha. A required slug absent from *source* reduces to ``unsigned`` with a
    ``None`` current sha (a template section the source never provided — nothing
    to sign). :attr:`ModuleAudit.passed` is true iff every required section is
    :data:`PASSING_STATUSES`.
    """
    records = read_decisions(experiment_dir, "notebook", audit_id)
    by_slug: dict[str, Section] = {s.slug: s for s in source.sections}
    audits: list[SectionAudit] = []
    for slug in required_slugs:
        section = by_slug.get(slug)
        current_sha = section.section_sha if section is not None else None
        audits.append(audit_section(records, slug, current_sha))
    passed = all(a.status in PASSING_STATUSES for a in audits)
    return ModuleAudit(sections=tuple(audits), passed=passed)


def record_auto_clear(
    experiment_dir: Path,
    *,
    audit_id: str,
    section: str,
    section_sha: str,
    recompute: Callable[[], str] | str,
    view_sha: str | None = None,
) -> dict[str, Any]:
    """Journal a CODE auto-clear attestation for *section*, un-fakeably.

    The writer this module owns for the code record class (called by the later
    gate/skill wave). It routes the record through the kernel's
    :func:`~hpc_agent.state.attestation.bind` recompute-lock BEFORE appending:
    the asserted *section_sha* must equal a freshly recomputed sha (*recompute*
    — the current sha parsed from the ``.py`` on disk, a string or a zero-arg
    callable), so a machine clearance can no more assert a sha into existence
    than a human can (D5 lock 2). Only after ``bind`` passes is the record
    appended.

    The record: ``block="notebook-auto-clear"``,
    ``response="auto_cleared"`` (the honest mechanical string — never a human-ack
    token), ``resolved={audit_id, section, section_sha, view_sha?, attestor:"code"}``.

    Returns the appended record. Raises :class:`errors.SpecInvalid` (via ``bind``)
    on a sha that does not match the recompute, or (via ``append_decision``) on a
    bad ``audit_id`` scope.
    """
    resolved: dict[str, Any] = {
        "audit_id": audit_id,
        "section": section,
        "section_sha": section_sha,
        "attestor": "code",
    }
    if view_sha:
        resolved["view_sha"] = view_sha
    # Un-fakeable lock: the asserted section_sha must match a fresh recompute
    # (routes through the ONE kernel; never re-inlined). Validates shape too.
    projected = _project({"block": AUTO_CLEAR_BLOCK, "resolved": resolved}) or {}
    attestation.bind(projected, recompute=recompute)
    return append_decision(
        experiment_dir,
        scope_kind="notebook",
        scope_id=audit_id,
        block=AUTO_CLEAR_BLOCK,
        response=AUTO_CLEAR_RESPONSE,
        resolved=resolved,
    )


# --- render receipts (T10) --------------------------------------------------
# A receipt is a THIRD block class riding the same journal. It is READ SEPARATELY
# from the sign-off / auto-clear reduction: :data:`_BLOCK_ATTESTOR` deliberately
# omits :data:`RENDER_RECEIPT_BLOCK`, so a receipt can never enter
# :func:`audit_section` and can never change the T6 status vocabulary. A receipt
# is evidence for the assertions-green leg of the D-attention TIER (read by
# :func:`~hpc_agent.ops.notebook.audit_view.build_audit_view`), not a clearance.


def _project_receipt(record: dict[str, Any]) -> dict[str, Any] | None:
    """Project a journal record to a render-receipt attestation dict, or ``None``.

    The receipt attestation binds/reduces on the SECTION sha
    (``content_sha == section_sha``) — this is what makes a receipt STALE by
    construction the moment its section drifts. The render's own ``output_sha``
    and ``error`` ride opaque ``evidence`` (never interpreted by the kernel).
    Returns ``None`` for any block other than :data:`RENDER_RECEIPT_BLOCK`, so a
    sign-off / auto-clear record is filtered out before the kernel sees it (and,
    symmetrically, a receipt never reaches the sign-off reducer).
    """
    if record.get("block") != RENDER_RECEIPT_BLOCK:
        return None
    resolved = record.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    return {
        "attestor": "code",
        "subject_kind": SUBJECT_KIND,
        "subject_id": resolved.get("section"),
        "content_sha": resolved.get("section_sha"),
        "evidence": {
            "output_sha": resolved.get("output_sha"),
            "error": resolved.get("error"),
        },
    }


def record_render_receipt(
    experiment_dir: Path,
    *,
    audit_id: str,
    section: str,
    section_sha: str,
    recompute: Callable[[], str] | str,
    output_sha: str,
    error: bool,
) -> dict[str, Any]:
    """Journal a CODE render receipt for *section*, bound to its sha, un-fakeably.

    A receipt asserts "this section's source was RENDERED (executed) and its
    declared assertions did/did not error" — the execution evidence the
    D-attention tier's assertions-green leg needs. It is NOT a clearance and NOT
    a sign-off; it carries the honest mechanical response :data:`RENDER_RECEIPT_RESPONSE`
    (``"rendered"``), never a human-ack token.

    Un-fakeable + fresh-by-construction: the receipt is bound through the ONE
    attestation kernel (:func:`~hpc_agent.state.attestation.bind`) against the
    SECTION sha, so a caller can no more assert a receipt for a sha the ``.py``
    does not currently carry than a human can assert a sign-off (D5 lock 2). And
    because the receipt records the section sha it was bound at, it reads STALE
    (see :func:`read_render_receipts`) the instant the section drifts — a receipt
    can only ever be recorded against current source.

    The record: ``block="notebook-render-receipt"``, ``response="rendered"``,
    ``resolved={audit_id, section, section_sha, output_sha, error, attestor:"code"}``.

    Returns the appended record. Raises :class:`errors.SpecInvalid` (via ``bind``)
    on a sha that does not match the recompute, or (via ``append_decision``) on a
    bad ``audit_id`` scope.
    """
    resolved: dict[str, Any] = {
        "audit_id": audit_id,
        "section": section,
        "section_sha": section_sha,
        "output_sha": output_sha,
        "error": error,
        "attestor": "code",
    }
    # Bind on the SECTION sha (routes through the ONE kernel; never re-inlined):
    # the receipt is stale-by-construction when the section moves, and can only
    # be recorded against the sha the source currently carries.
    projected = _project_receipt({"block": RENDER_RECEIPT_BLOCK, "resolved": resolved}) or {}
    attestation.bind(projected, recompute=recompute)
    return append_decision(
        experiment_dir,
        scope_kind="notebook",
        scope_id=audit_id,
        block=RENDER_RECEIPT_BLOCK,
        response=RENDER_RECEIPT_RESPONSE,
        resolved=resolved,
    )


def read_render_receipts(
    experiment_dir: Path,
    audit_id: str,
    *,
    current_shas: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Read the newest-valid render receipt per section, with a freshness flag.

    Returns ``{slug: {output_sha, error, section_sha, fresh}}`` — one entry per
    section that carries at least one VALID receipt. ``section_sha`` is the sha
    the newest valid receipt was bound at; ``fresh`` is ``True`` iff that sha
    still equals *current_shas[slug]* (the section has not drifted since it was
    rendered). A section absent from *current_shas*, or whose recorded sha no
    longer matches, reads ``fresh=False`` — a stale receipt is no evidence.

    Newest-valid selection and the freshness (drift) verdict both route through
    the SAME kernel machinery every other attestation reader uses
    (:func:`_newest_valid` + :func:`~hpc_agent.state.attestation.reduce`), never
    a re-inlined newest-first / sha-compare. Malformed receipt records are
    skipped, never fatal.
    """
    records = read_decisions(experiment_dir, "notebook", audit_id)
    projected: list[dict[str, Any]] = []
    for record in records:
        receipt = _project_receipt(record)
        if receipt is not None:
            projected.append(receipt)

    out: dict[str, dict[str, Any]] = {}
    slugs = {p["subject_id"] for p in projected if isinstance(p.get("subject_id"), str)}
    for slug in slugs:
        newest = _newest_valid(projected, slug)
        if newest is None:
            continue
        current = current_shas.get(slug)
        # Route the freshness (current/stale) verdict through the ONE kernel.
        fresh = (
            current is not None
            and attestation.reduce(projected, current_sha=current, subject_id=slug)
            == attestation.CURRENT
        )
        evidence = newest.evidence if isinstance(newest.evidence, dict) else {}
        out[slug] = {
            "output_sha": evidence.get("output_sha"),
            "error": evidence.get("error"),
            "section_sha": newest.content_sha,
            "fresh": fresh,
        }
    return out


# --- draft attestations (multi-human MH5) ------------------------------------
# A block class riding the same journal, READ SEPARATELY from the sign-off /
# auto-clear reduction: its projection is NOT registered in :data:`_BLOCK_ATTESTOR`,
# so a draft can never enter :func:`audit_section` and can never change the T6
# status vocabulary (a draft is authorship provenance, not a clearance). It rides
# a DISTINCT :data:`DRAFT_SUBJECT_KIND` and carries the drafting session's actor
# as the attestation's ``attestor_id`` (WHICH actor — opaque, harness-asserted,
# never verified). The reviewer!=author gate (MH6) resolves the section AUTHOR by
# reducing these draft records at the current sha (:func:`read_draft_author`).


def _project_draft(record: dict[str, Any]) -> dict[str, Any] | None:
    """Project a journal record to a DRAFT attestation dict, or ``None``.

    The draft attestation binds/reduces on the SECTION sha
    (``content_sha == section_sha``) — this is what makes an OLD draft read STALE
    the moment its section is redrafted, so authorship follows the CURRENT content
    (the D8 no-state-machine property). The drafting session's actor rides
    ``attestor_id`` (opaque, absent when the draft was unattributed — zero/one
    declared actor). Returns ``None`` for any block other than :data:`DRAFT_BLOCK`,
    so a sign-off / auto-clear / receipt record is filtered out before the kernel
    sees it (and, symmetrically, a draft never reaches the sign-off reducer).
    """
    if record.get("block") != DRAFT_BLOCK:
        return None
    resolved = record.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    projected: dict[str, Any] = {
        "attestor": "code",
        "subject_kind": DRAFT_SUBJECT_KIND,
        "subject_id": resolved.get("section"),
        "content_sha": resolved.get("section_sha"),
    }
    actor = resolved.get("actor")
    if actor:
        # WHICH actor drafted — opaque slug, stamped only when the session was
        # attributed. An unattributed draft (zero/one declared actor) carries no
        # attestor_id, so it validates byte-compatibly as a single-actor record.
        projected["attestor_id"] = actor
    return projected


def record_draft(
    experiment_dir: Path,
    *,
    audit_id: str,
    section: str,
    section_sha: str,
    recompute: Callable[[], str] | str,
    actor: str | None,
) -> dict[str, Any]:
    """Journal a CODE draft attestation for *section*, bound to its sha, un-fakeably.

    A draft attestation records "the actor whose SESSION recorded this draft, at
    this section sha" — the SECTION AUTHOR the reviewer!=author gate (MH6) needs,
    recorded at DRAFT time by the drafting session, never reconstructed at review
    time. It is NOT a clearance and NOT a sign-off; it carries the honest
    mechanical response :data:`DRAFT_RESPONSE` (``"drafted"``), never a human-ack
    token.

    Un-fakeable + fresh-by-construction: the draft is bound through the ONE
    attestation kernel (:func:`~hpc_agent.state.attestation.bind`) against the
    SECTION sha, so a caller can no more assert a draft for a sha the ``.py`` does
    not currently carry than a human can assert a sign-off (D5 lock 2). Because
    the record binds the section sha, a REDRAFT (which moves the sha) leaves the
    old draft STALE via the ONE reducer (:func:`read_draft_author`) — authorship
    follows the current content with no state machine. The *actor* is
    harness-asserted from outside the model's tool surface (``HPC_ACTOR``), never
    caller-asserted on the wire; ``None`` records an unattributed draft (zero/one
    declared actor) with no ``attestor_id`` — comparisons stay off.

    The record: ``block="notebook-draft"``, ``response="drafted"``,
    ``resolved={audit_id, section, section_sha, actor?}`` (``actor`` present only
    when attributed).

    Returns the appended record. Raises :class:`errors.SpecInvalid` (via ``bind``)
    on a sha that does not match the recompute, or (via ``append_decision``) on a
    bad ``audit_id`` scope.
    """
    resolved: dict[str, Any] = {
        "audit_id": audit_id,
        "section": section,
        "section_sha": section_sha,
    }
    if actor is not None:
        resolved["actor"] = actor
    # Bind on the SECTION sha (routes through the ONE kernel; never re-inlined):
    # the draft is stale-by-construction when the section moves, and can only be
    # recorded against the sha the source currently carries.
    projected = _project_draft({"block": DRAFT_BLOCK, "resolved": resolved}) or {}
    attestation.bind(projected, recompute=recompute)
    return append_decision(
        experiment_dir,
        scope_kind="notebook",
        scope_id=audit_id,
        block=DRAFT_BLOCK,
        response=DRAFT_RESPONSE,
        resolved=resolved,
    )


def read_draft_author(
    experiment_dir: Path, audit_id: str, section: str, *, current_sha: str
) -> str | None:
    """The actor who drafted *section* at *current_sha*, or ``None``.

    Reads *audit_id*'s notebook journal, projects the draft records, and returns
    the ``attestor_id`` (drafting actor) of the newest VALID draft for *section*
    — but ONLY when that draft is CURRENT at *current_sha*. A redrafted section
    whose newest draft binds an older sha reads STALE and yields ``None`` (no
    author at the current content), exactly the property MH6 relies on: a stale
    draft attribution is no attribution. Both the newest-valid selection and the
    current/stale verdict route through the SAME kernel machinery every other
    attestation reader uses (:func:`_newest_valid` + :func:`reduce`), never a
    re-inlined newest-first / sha-compare. Malformed draft records are skipped,
    never fatal.

    Returns ``None`` when there is no current draft OR when the current draft was
    unattributed (no ``attestor_id`` — a zero/one-actor draft). The MH6 gate
    distinguishes "no draft" from "unattributed draft" by whether ``>1`` actor is
    declared; both read ``None`` here.
    """
    records = read_decisions(experiment_dir, "notebook", audit_id)
    projected = [p for p in (_project_draft(r) for r in records) if p is not None]
    newest = _newest_valid(projected, section)
    if newest is None:
        return None
    verdict = attestation.reduce(projected, current_sha=current_sha, subject_id=section)
    if verdict != attestation.CURRENT:
        return None
    # attestor_id (MH3) is the drafting actor; getattr keeps this readable against
    # a mypy env pinned to a pre-multi-human Attestation shape (installed-pkg skew).
    author: str | None = getattr(newest, "attestor_id", None)
    return author


# --- audit config record (run-#10 standalone-audit seat) ---------------------
# A FOURTH block class riding the same journal. It is NOT an attestation (no
# section, no content_sha) and never enters the sign-off / receipt reductions:
# :data:`_BLOCK_ATTESTOR` and :func:`_project_receipt` both omit
# :data:`AUDIT_CONFIG_BLOCK` by construction. The IMMUTABILITY posture (one
# config per audit_id; superseding = a new audit_id) is enforced by the
# ``notebook-record-config`` verb's refusal; the reader mirrors it by taking the
# FIRST valid record — a later hand-appended line can never supersede the one
# the verb recorded.


def record_audit_config(
    experiment_dir: Path,
    *,
    audit_id: str,
    input_roots: Sequence[str],
    source_roots: Sequence[str],
    attention_order: Sequence[str] | None = None,
    output_roots: Sequence[str] = (),
    goal: str | None = None,
    task_axes: Sequence[str] | None = None,
    observables: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Journal the audit-OPEN seat for a STANDALONE audit: config + intent.

    Appends the ``notebook-audit-config`` record —
    ``resolved={audit_id, input_roots, source_roots, attention_order,
    output_roots, observables, goal?, task_axes?}``,
    ``response="config_recorded"`` — to *audit_id*'s notebook journal. Roots
    are OPAQUE relpath strings and ``observables`` are opaque
    declared-observable names (the A14 observation plan) — core attaches no
    meaning. This writer does NOT check for a prior config record or an
    interview ``audited_source`` block — the ``notebook-record-config`` verb
    owns those refusals (one source of truth; immutable-per-audit).

    ``goal`` and ``task_axes`` are the audit-OPEN INTENT utterances the human
    typed (the free-text campaign goal and the free-text names of what varies
    across tasks — e.g. ``["bucket", "chunk"]``). They are the durable seat the
    ``audit-handoff`` projection reads to draft an ``InterviewSpec`` — the
    prerequisite the ``docs/design/notebook-audit.md`` audit-handoff note names
    (before this, the intent lived only in chat). They ride the SAME config
    record (one audit-open seat, immutable-per-audit) and are recorded VERBATIM
    — never interpreted, never invented (an omitted answer stays omitted, the
    projection discloses the gap and emits a placeholder). Both are appended
    ONLY when supplied, so a config record written WITHOUT them is byte-identical
    to a pre-intent record (the D7 fail-safe: an audit that never opts into the
    handoff seat is unchanged).

    Returns the appended record. Raises :class:`errors.SpecInvalid` (via
    ``append_decision``) on a bad ``audit_id`` scope.
    """
    resolved: dict[str, Any] = {
        "audit_id": audit_id,
        "input_roots": list(input_roots),
        "source_roots": list(source_roots),
        "attention_order": list(attention_order) if attention_order is not None else None,
        "output_roots": list(output_roots),
        "observables": list(observables) if observables is not None else None,
    }
    # Intent utterances ride the same audit-open seat, appended only when the
    # human supplied them — an omitted field stays absent so a config-only record
    # is byte-identical to a pre-intent one (never a fabricated empty goal).
    if goal is not None:
        resolved["goal"] = goal
    if task_axes is not None:
        resolved["task_axes"] = list(task_axes)
    return append_decision(
        experiment_dir,
        scope_kind="notebook",
        scope_id=audit_id,
        block=AUDIT_CONFIG_BLOCK,
        response=AUDIT_CONFIG_RESPONSE,
        resolved=resolved,
    )


def read_audit_intent(experiment_dir: Path, audit_id: str) -> tuple[str | None, list[str]]:
    """The audit-OPEN intent utterances ``(goal, task_axes)`` for *audit_id*.

    Reads the FIRST valid ``notebook-audit-config`` record (the immutable seat
    :func:`read_audit_config` reads) and projects its intent fields: ``goal``
    (the free-text campaign goal, or ``None`` when the audit-open seat recorded
    no goal) and ``task_axes`` (the free-text compute-shape axis names, ``[]``
    when none were recorded). Both are OPAQUE — never interpreted by core. A
    record with no intent fields, or no config record at all, reads ``(None,
    [])`` — the projection discloses the gap rather than guessing.
    """
    resolved = read_audit_config(experiment_dir, audit_id)
    if resolved is None:
        return None, []
    goal = resolved.get("goal")
    goal = goal if isinstance(goal, str) and goal else None
    raw_axes = resolved.get("task_axes")
    task_axes = [str(a) for a in raw_axes if str(a)] if isinstance(raw_axes, list) else []
    return goal, task_axes


def read_audit_config(experiment_dir: Path, audit_id: str) -> dict[str, Any] | None:
    """The journaled audit-config ``resolved`` mapping for *audit_id*, or ``None``.

    FIRST valid record wins (the immutability posture: the verb refuses a second
    record, so first == only; a hand-appended later line never supersedes).
    A record whose ``resolved`` is not a dict is skipped, never fatal.
    """
    for record in read_decisions(experiment_dir, "notebook", audit_id):
        if record.get("block") != AUDIT_CONFIG_BLOCK:
            continue
        resolved = record.get("resolved")
        if isinstance(resolved, dict):
            return resolved
    return None


# --- relay-due markers (the omission gate) -----------------------------------
# A FOURTH and FIFTH block class riding the same journal: a relay-due MARKER
# (journaled by ``notebook-status`` when it computes a TERMINAL audit state) and
# its DISCHARGE (journaled by the relay-audit Stop hook when the final assistant
# text actually carried one of the marker's key tokens). ``verify-relay`` and
# the Stop hook's contradiction pass audit what WAS said; this pair enforces
# what MUST be said — the omission side of the relay boundary. Like the render
# receipt, both blocks are deliberately ABSENT from :data:`_BLOCK_ATTESTOR` and
# from :func:`_project_receipt`, so a marker/discharge can never enter the
# attestation reduction or the receipt reader. Append-only discipline: a
# discharge NEVER mutates its marker — it is a second record whose key names
# the first.

#: The relay-due marker block — "this terminal verdict has not reached the
#: human yet". Written by the ``notebook-status`` op on a terminal state.
RELAY_DUE_BLOCK = "notebook-relay-due"

#: The honest, mechanical ``response`` a relay-due marker carries — an
#: obligation was recorded, nothing was approved or relayed.
RELAY_DUE_RESPONSE = "relay_due"

#: The one v1 ``record_kind`` — ONLY ``notebook-status`` terminals set markers
#: (the narrow set is deliberate: marking everything relay-due recreates alarm
#: fatigue inside the enforcement itself).
RELAY_DUE_RECORD_KIND = "notebook-status"

#: The SECOND ``record_kind`` (run-#11 item 3, "a link is not a relay"). The
#: ``notebook-audit-view`` verb arms a per-section marker when it builds the
#: CANONICAL view of a HUMAN-REQUIRED section: its single key token is the
#: section's ``view_sha12`` (the hash embedded in the trusted render filename),
#: so the marker discharges only when that sha12 actually appears in the turn —
#: the render reached the human as content, not as an unread file link. Still
#: the narrow set: preview views and auto_cleared sections arm nothing.
RENDER_RELAY_DUE_RECORD_KIND = "notebook-audit-view"

#: The discharge block — "the final assistant text carried a key token of the
#: named marker". Written by the relay-audit Stop hook, never by hand.
RELAY_DISCHARGE_BLOCK = "notebook-relay-discharge"

#: The honest, mechanical ``response`` a discharge carries.
RELAY_DISCHARGE_RESPONSE = "relay_discharged"


def _marker_key(resolved: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The identity of one relay-due marker, or ``None`` for a malformed record.

    ``(record_kind, audit_id, key_tokens..., created_at)`` — the fields a
    discharge record echoes back. Malformed shapes (non-string fields, a
    non-list ``key_tokens``) yield ``None`` and are skipped by every reader:
    one bad line never strands the rest of the audit trail, and never raises.
    """
    record_kind = resolved.get("record_kind")
    audit_id = resolved.get("audit_id")
    key_tokens = resolved.get("key_tokens")
    created_at = resolved.get("created_at")
    if not (isinstance(record_kind, str) and record_kind):
        return None
    if not (isinstance(audit_id, str) and audit_id):
        return None
    if not (isinstance(created_at, str) and created_at):
        return None
    if not isinstance(key_tokens, list) or not key_tokens:
        return None
    if not all(isinstance(t, str) and t for t in key_tokens):
        return None
    return (record_kind, audit_id, tuple(key_tokens), created_at)


def record_relay_due(
    experiment_dir: Path,
    *,
    audit_id: str,
    state: str,
    module_sha: str,
) -> dict[str, Any] | None:
    """Journal a relay-due marker for a TERMINAL ``notebook-status`` verdict.

    The marker's ``resolved`` is the design shape: ``{record_kind:
    "notebook-status", audit_id, key_tokens: [<state word>, <module sha12>],
    created_at}``. Any one key token appearing (case-insensitive substring) in
    the final assistant text discharges the obligation — the state word or the
    sha12 both identify the verdict.

    Deduplicated on ``(record_kind, key_tokens)``: the same terminal fact
    (same state at the same module sha) is ONE relay obligation, however many
    times ``notebook-status`` recomputes it — this is what keeps the op's
    ``idempotent=True`` honest and keeps the audit loop from stacking
    obligations (alarm fatigue inside the enforcement). Returns the appended
    record, or ``None`` when an identical marker already exists (discharged or
    not: an already-relayed fact does not re-arm).
    """
    key_tokens = [state, module_sha[:12]]
    for record in read_decisions(experiment_dir, "notebook", audit_id):
        if record.get("block") != RELAY_DUE_BLOCK:
            continue
        resolved = record.get("resolved")
        key = _marker_key(resolved) if isinstance(resolved, dict) else None
        if key is not None and key[0] == RELAY_DUE_RECORD_KIND and key[2] == tuple(key_tokens):
            return None
    resolved_out: dict[str, Any] = {
        "record_kind": RELAY_DUE_RECORD_KIND,
        "audit_id": audit_id,
        "key_tokens": key_tokens,
        "created_at": _utcnow_iso(),
    }
    return append_decision(
        experiment_dir,
        scope_kind="notebook",
        scope_id=audit_id,
        block=RELAY_DUE_BLOCK,
        response=RELAY_DUE_RESPONSE,
        resolved=resolved_out,
    )


def record_scope_relay_due(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    record_kind: str,
    key_tokens: Sequence[str],
) -> dict[str, Any] | None:
    """Journal a relay-due marker on an arbitrary journal scope (run-#10 #13).

    The generalized writer behind :func:`record_relay_due` — campaign/run
    terminal outcomes arm the same omission gate the notebook loop uses
    (``ops/campaign_run.py`` is the first caller: every terminal
    ``stage_reached`` of an iteration is relay-due). The marker's ``resolved``
    keeps the ``audit_id`` FIELD NAME carrying *scope_id* — a documented wart
    that buys zero migration: the hook's ``_marker_key`` and the discharge
    echo work unchanged across scopes. Same dedup rule as the notebook
    writer: an identical ``(record_kind, key_tokens)`` marker (discharged or
    not) does not re-arm.
    """
    tokens = [str(t) for t in key_tokens if str(t)]
    if not tokens:
        return None
    for record in read_decisions(experiment_dir, scope_kind, scope_id):
        if record.get("block") != RELAY_DUE_BLOCK:
            continue
        resolved = record.get("resolved")
        key = _marker_key(resolved) if isinstance(resolved, dict) else None
        if key is not None and key[0] == record_kind and key[2] == tuple(tokens):
            return None
    resolved_out: dict[str, Any] = {
        "record_kind": record_kind,
        "audit_id": scope_id,
        "key_tokens": tokens,
        "created_at": _utcnow_iso(),
    }
    return append_decision(
        experiment_dir,
        scope_kind=scope_kind,
        scope_id=scope_id,
        block=RELAY_DUE_BLOCK,
        response=RELAY_DUE_RESPONSE,
        resolved=resolved_out,
    )


def read_undischarged_relay_markers(
    experiment_dir: Path,
    audit_id: str,
    scope_kind: str = "notebook",
) -> list[dict[str, Any]]:
    """Every relay-due marker ``resolved`` dict with no matching discharge.

    A marker is discharged when a :data:`RELAY_DISCHARGE_BLOCK` record whose
    identity fields (``record_kind``, ``audit_id``, ``key_tokens``,
    ``created_at``) echo the marker's exists anywhere in the journal — the
    original marker is never mutated (append-only store). Malformed marker or
    discharge lines are skipped, never raised (the fail-open posture the Stop
    hook depends on).
    """
    markers: list[dict[str, Any]] = []
    discharged: set[tuple[Any, ...]] = set()
    for record in read_decisions(experiment_dir, scope_kind, audit_id):
        block = record.get("block")
        resolved = record.get("resolved")
        if not isinstance(resolved, dict):
            continue
        key = _marker_key(resolved)
        if key is None:
            continue
        if block == RELAY_DUE_BLOCK:
            markers.append(resolved)
        elif block == RELAY_DISCHARGE_BLOCK:
            discharged.add(key)
    return [m for m in markers if _marker_key(m) not in discharged]


#: The two discharge provenances (D3, ``docs/design/stop-hook-completer.md``).
#: ``"relay"`` — the model's final text carried the marker's key token, so the
#: human saw a MODEL relay. ``"completer"`` — the Stop-hook COMPLETER appended
#: the owed artifact itself (a code-untouched render/verdict), so the human saw
#: CODE-AUTHORED text. The journal-derived count of ``completer`` vs ``relay``
#: discharges is the automatability metric: how much extra-model-turn latency the
#: completer killed. Records written before D3 carry no field and read ``"relay"``.
DISCHARGED_BY_RELAY = "relay"
DISCHARGED_BY_COMPLETER = "completer"


def record_relay_discharge(
    experiment_dir: Path,
    *,
    audit_id: str,
    marker: Mapping[str, Any],
    discharged_at: str | None = None,
    scope_kind: str = "notebook",
    discharged_by: str = DISCHARGED_BY_RELAY,
) -> dict[str, Any]:
    """Journal the discharge of one relay-due *marker* (append-only, no mutate).

    ``resolved`` echoes the marker's identity fields verbatim (``record_kind``,
    ``audit_id``, ``key_tokens``, ``created_at``) plus ``discharged_at`` — the
    marker key + the discharge stamp, exactly — and ``discharged_by`` (D3): the
    provenance of the discharge, ``"relay"`` (the model relayed the token) or
    ``"completer"`` (the Stop-hook completer code-appended the owed artifact).
    The field is additive; a discharge written before D3 reads ``"relay"``, and
    ``discharged_by`` is NOT part of the marker identity (``_marker_key``), so it
    never changes which marker a discharge closes. Raises
    :class:`~hpc_agent.errors.SpecInvalid` on a malformed *marker* (a discharge
    must name a real marker identity, never a hand-rolled shape).
    """
    from hpc_agent import errors

    key = _marker_key(marker)
    if key is None:
        raise errors.SpecInvalid(
            "record_relay_discharge: marker must carry record_kind, audit_id, "
            f"key_tokens, created_at; got {dict(marker)!r}"
        )
    resolved: dict[str, Any] = {
        "record_kind": marker["record_kind"],
        "audit_id": marker["audit_id"],
        "key_tokens": list(marker["key_tokens"]),
        "created_at": marker["created_at"],
        "discharged_at": discharged_at or _utcnow_iso(),
        "discharged_by": discharged_by,
    }
    return append_decision(
        experiment_dir,
        scope_kind=scope_kind,
        scope_id=audit_id,
        block=RELAY_DISCHARGE_BLOCK,
        response=RELAY_DISCHARGE_RESPONSE,
        resolved=resolved,
    )


def _utcnow_iso() -> str:
    """The journal's timestamp convention (one definition, ``infra.time``)."""
    from hpc_agent.infra.time import utcnow_iso

    return utcnow_iso()
