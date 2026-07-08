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
    "AUDIT_CONFIG_BLOCK",
    "SUBJECT_KIND",
    "AUTO_CLEAR_RESPONSE",
    "RENDER_RECEIPT_RESPONSE",
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
    "record_audit_config",
    "read_audit_config",
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
) -> dict[str, Any]:
    """Journal the audit configuration for a STANDALONE audit.

    Appends the ``notebook-audit-config`` record —
    ``resolved={audit_id, input_roots, source_roots, attention_order, output_roots}``,
    ``response="config_recorded"`` — to *audit_id*'s notebook journal. Roots are
    OPAQUE relpath strings (core attaches no meaning). This writer does NOT
    check for a prior config record or an interview ``audited_source`` block —
    the ``notebook-record-config`` verb owns those refusals (one source of
    truth; immutable-per-audit).

    Returns the appended record. Raises :class:`errors.SpecInvalid` (via
    ``append_decision``) on a bad ``audit_id`` scope.
    """
    resolved: dict[str, Any] = {
        "audit_id": audit_id,
        "input_roots": list(input_roots),
        "source_roots": list(source_roots),
        "attention_order": list(attention_order) if attention_order is not None else None,
        "output_roots": list(output_roots),
    }
    return append_decision(
        experiment_dir,
        scope_kind="notebook",
        scope_id=audit_id,
        block=AUDIT_CONFIG_BLOCK,
        response=AUDIT_CONFIG_RESPONSE,
        resolved=resolved,
    )


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
