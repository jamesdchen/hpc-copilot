"""Graduation gate — refuse a submit whose audited ``.py`` is not signed current.

The notebook-audit substrate's D8 gate (``docs/design/notebook-audit.md``): ONE
definition, two synchronous seats. This module is that ONE definition;
:func:`assert_source_audited` is called at
:mod:`hpc_agent.ops.resolve_submit_inputs` (pre-sidecar, the S1 human boundary)
and :mod:`hpc_agent.ops.submit_flow` (pre-staging, before any rsync/SSH) — the
same defense-in-depth / gate-before-cluster-work pattern the scope gate
(:mod:`hpc_agent.ops.scope_gate`) wires at its two seats.

Opt-in + fail-safe, the ``ops/scope_gate.py`` posture copied exactly (D7): with
NO ``audited_source`` block on ``interview.json`` the gate RETURNS silently and
byte-identically — zero filesystem probes beyond the single ``interview.json``
read (the seats already read that file). It fires ONLY inside the opted-in
surface. An opted-in repo whose declared source/template ``.py`` is missing or
unparseable is BROKEN, not a silent pass — that is a LOUD :class:`errors.SpecInvalid`
naming the path (mirrors the T8 sign-off gate's unresolvable-source refusal).

Drift = unsigned by construction (D8): a section signed then edited simply reads
unsigned at its new hash — there is no drift state machine. The
:func:`~hpc_agent.state.notebook_audit.audit_module` reduction owns the
per-section verdict; this gate adds one more revocation the reduction cannot see
— a **drifted linked source**: a passing section whose newest sign-off recorded
``linked_sources`` (T4's ``{module, file, module_sha}`` convention) reads unsigned
if any linked file no longer matches its recorded ``sha256_normalized`` (a
changed imported dependency revokes the section's trust).

Pure local reads — no SSH, no ``_wire`` import, no scheduler.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.notebook_audit import (
    AUTO_CLEAR_BLOCK,
    PASSING_STATUSES,
    SIGN_OFF_BLOCK,
    audit_module,
)

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.state.audit_source import ParsedModule

__all__ = ["assert_source_audited", "audit_currency", "audited_source_echo"]

#: The two notebook-attestation blocks a sign-off/auto-clear record can carry —
#: used to locate the winning record for a passing section's linked-source check.
_NOTEBOOK_BLOCKS = frozenset({SIGN_OFF_BLOCK, AUTO_CLEAR_BLOCK})


def _read_audited_source(experiment_dir: Path) -> dict[str, Any] | None:
    """The interview.json ``audited_source`` block, or ``None`` when not opted in.

    Mirrors :func:`hpc_agent.ops.decision.journal._read_interview_audited_source`'s
    posture (the canonical campaign-dir root, ``.hpc/interview.json`` accepted
    defensively — the ``detect_entry_point`` convention). A missing file, a
    corrupt/non-object file, or an absent ``audited_source`` key all read as "not
    opted in" → ``None`` → the D7 silent no-op. This is the ONLY filesystem probe
    on the not-opted-in path.
    """
    for rel in ("interview.json", ".hpc/interview.json"):
        path = experiment_dir / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        block = doc.get("audited_source")
        if isinstance(block, dict):
            return block
    return None


def _read_required_py(experiment_dir: Path, rel: Any, *, kind: str, audit_id: Any) -> str:
    """Read a REQUIRED ``.py`` (source or template) at *rel*, or refuse LOUDLY.

    An opted-in repo with an unresolvable *kind* is broken (the gate RECOMPUTES
    hashes from the ``.py`` on disk), so a missing path field or an unreadable
    file raises :class:`errors.SpecInvalid` naming *rel* — never a silent pass
    (this is the opted-in surface; D7 silence applies only to the ABSENT
    ``audited_source`` block, resolved earlier).
    """
    if not isinstance(rel, str) or not rel:
        raise errors.SpecInvalid(
            f"notebook graduation gate: audited_source (audit_id {audit_id!r}) "
            f"declares no {kind} .py path. An opted-in repo with an unresolvable "
            f"{kind} is broken — fix interview.json's audited_source block."
        )
    path = experiment_dir / rel
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"notebook graduation gate: audited {kind} {rel!r} is unreadable "
            f"({exc}). The gate recomputes section hashes from the .py on disk; "
            f"an opted-in repo with a missing/unreadable {kind} is broken, not a "
            "silent pass."
        ) from exc


def _parse_required(rel: str, text: str, *, kind: str, audit_id: Any) -> ParsedModule:
    """Parse a REQUIRED ``.py`` into its section model, re-wrapping the loud refusal.

    :func:`parse_percent_source` already raises :class:`errors.SpecInvalid` on a
    malformed marker; this adds the path + kind + audit_id context so the human
    knows WHICH opted-in file is broken.
    """
    try:
        return parse_percent_source(text)
    except errors.SpecInvalid as exc:
        raise errors.SpecInvalid(
            f"notebook graduation gate: audited {kind} {rel!r} (audit_id "
            f"{audit_id!r}) is not valid percent-format source: {exc}"
        ) from exc


def _winning_record(
    records: list[dict[str, Any]], slug: str, section_sha: str | None
) -> dict[str, Any] | None:
    """The newest notebook record for *slug* attesting *section_sha*, or ``None``.

    For a PASSING section, :func:`~hpc_agent.state.notebook_audit.audit_module`
    already resolved the winning attestation and reported its ``signed_section_sha``;
    this locates that exact record (newest-first) so its ``linked_sources`` can be
    drift-checked. Never re-derives the pass verdict — pure record selection.
    """
    if section_sha is None:
        return None
    for record in reversed(records):
        if record.get("block") not in _NOTEBOOK_BLOCKS:
            continue
        resolved = record.get("resolved")
        if not isinstance(resolved, dict):
            continue
        if resolved.get("section") == slug and resolved.get("section_sha") == section_sha:
            return record
    return None


def _linked_source_drift(experiment_dir: Path, record: dict[str, Any] | None) -> str | None:
    """Return a description of the FIRST drifted/missing linked source, or ``None``.

    Reads the sign-off *record*'s ``resolved['linked_sources']`` (T4's
    ``{module, file, module_sha}`` list) and recomputes
    :func:`~hpc_agent.state.audit_source.sha256_normalized` over each linked file.
    A missing file or a sha mismatch means the section's cleared dependency
    changed after sign-off — trust is revoked (the section reads unsigned). A
    record with no ``linked_sources`` (the common case) never drifts.
    """
    if record is None:
        return None
    resolved = record.get("resolved")
    linked = resolved.get("linked_sources") if isinstance(resolved, dict) else None
    if not isinstance(linked, list):
        return None
    for link in linked:
        if not isinstance(link, dict):
            continue
        rel = link.get("file")
        expected = link.get("module_sha")
        if not isinstance(rel, str) or not rel or not isinstance(expected, str) or not expected:
            continue
        path = experiment_dir / rel
        try:
            actual = sha256_normalized(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return f"{rel} missing"
        if actual != expected:
            return f"{rel} changed"
    return None


def audited_source_echo(experiment_dir: Path) -> dict[str, Any] | None:
    """The sidecar-sealable slice of interview.json's ``audited_source``, or ``None``.

    Returns ``{source, template, audit_id}`` — the source ``.py`` relpath, the
    template ``.py`` relpath, and the opaque audit slug — for the run sidecar to
    echo (notebook-audit T14) so ``export-dossier`` can seal the audit trail.
    ``rendered_notebook`` is deliberately DROPPED: it is a caller-side render
    metadatum, never a sealed record. Reuses :func:`_read_audited_source` — the
    ONE definition of the interview-block read — so the echo can never diverge
    from what :func:`assert_source_audited` audited. ``None`` (not opted in) → the
    caller omits the field → the sidecar stays byte-identical (the D7 fail-safe
    carried onto the echo). Pure local read — no SSH.
    """
    block = _read_audited_source(experiment_dir)
    if block is None:
        return None
    echo: dict[str, Any] = {key: block.get(key) for key in ("source", "template", "audit_id")}

    # S4 domain-pack echo (docs/design/domain-packs.md, T9d): when the audited
    # TEMPLATE .py is itself a file in a CURRENT-bound pack's manifest, additively
    # stamp the opaque {pack, version, sha} echo so export-dossier can prove WHICH
    # pack's template gated the audit. FAIL-OPEN + cheap (the gate's read posture):
    # no packs opt-in, a template in no bound pack, or any dangling/drifted pack
    # reference → NO ``pack`` key → a byte-identical echo (the D7 silence). Core
    # copies the echo verbatim; it never reads it back for meaning.
    template = echo.get("template")
    if isinstance(template, str) and template:
        from hpc_agent.state.pack_declarations import resolve_template_pack_echo

        pack_echo = resolve_template_pack_echo(experiment_dir, template)
        if pack_echo is not None:
            echo["pack"] = pack_echo
    return echo


def audit_currency(experiment_dir: Path) -> tuple[str, int] | None:
    """The opted-in audit's currency for the S1 DISCLOSURE — ``(audit_id, moved)``.

    Run #11 mechanization item 1 (audit-currency disclosure). Reuses the SAME
    computation ``notebook-status`` uses — :func:`~hpc_agent.state.notebook_audit.audit_module`
    reducing every REQUIRED (template) section against the ``audit_id`` journal —
    so nothing here re-implements hashing. Reads ``interview.json``'s
    ``audited_source`` block through the ONE definition (:func:`_read_audited_source`);
    NOT opted in → ``None`` (the D7 silence). Opted in → returns ``(audit_id,
    moved)`` where ``moved`` counts the required sections NOT signed-current
    (:data:`~hpc_agent.state.notebook_audit.PASSING_STATUSES`) — ``moved == 0`` ⇔
    the audit is current.

    DISCLOSURE seam only — it mirrors the notebook-status verdict, NOT the
    graduation refusal: it deliberately does NOT apply the gate's extra
    linked-source drift revocation (:func:`_linked_source_drift`), exactly as
    ``notebook-status`` does not. :func:`assert_source_audited` stays the single
    refusing seat. Like that gate it raises LOUDLY on a BROKEN opted-in repo
    (missing / unreadable / unparseable ``.py``); the disclosure caller
    (``ops/resolve_submit_inputs``) wraps this in a fail-open guard, so a crash
    degrades to disclosed-absent rather than an S1 error.

    Pure local reads — no SSH.
    """
    block = _read_audited_source(experiment_dir)
    if block is None:
        return None  # D7 silence — not opted in

    audit_id = block.get("audit_id")
    source_text = _read_required_py(
        experiment_dir, block.get("source"), kind="source", audit_id=audit_id
    )
    template_text = _read_required_py(
        experiment_dir, block.get("template"), kind="template", audit_id=audit_id
    )
    parsed_source = _parse_required(
        str(block.get("source")), source_text, kind="source", audit_id=audit_id
    )
    parsed_template = _parse_required(
        str(block.get("template")), template_text, kind="template", audit_id=audit_id
    )
    audit = audit_module(
        experiment_dir,
        str(audit_id),
        source=parsed_source,
        required_slugs=parsed_template.slugs,
    )
    moved = sum(1 for sect in audit.sections if sect.status not in PASSING_STATUSES)
    return str(audit_id), moved


def assert_source_audited(experiment_dir: Path) -> None:
    """Refuse a submit whose opted-in audited ``.py`` is not signed at its current hash.

    Loads ``interview.json``'s ``audited_source`` block. ABSENT (not opted in) →
    RETURN silently, byte-identically (D7 fail-safe — no further filesystem
    probes). PRESENT → parse the source + template ``.py`` (a missing/unparseable
    file is a LOUD :class:`errors.SpecInvalid` naming the path), reduce every
    REQUIRED (template) section via
    :func:`~hpc_agent.state.notebook_audit.audit_module`, and drift-check any
    ``linked_sources`` recorded on each PASSING section's winning sign-off. Any
    required section not signed-current (unsigned, drifted, or linked-source
    revoked) raises :class:`errors.SourceUnaudited` NAMING every offending section
    and its status.

    Pure local reads — no SSH. The two submit seats call this ONE definition.
    """
    block = _read_audited_source(experiment_dir)
    if block is None:
        return  # D7 fail-safe: not opted in → byte-identical no-op

    audit_id = block.get("audit_id")
    source_text = _read_required_py(
        experiment_dir, block.get("source"), kind="source", audit_id=audit_id
    )
    template_text = _read_required_py(
        experiment_dir, block.get("template"), kind="template", audit_id=audit_id
    )
    parsed_source = _parse_required(
        str(block.get("source")), source_text, kind="source", audit_id=audit_id
    )
    parsed_template = _parse_required(
        str(block.get("template")), template_text, kind="template", audit_id=audit_id
    )

    audit = audit_module(
        experiment_dir,
        str(audit_id),
        source=parsed_source,
        required_slugs=parsed_template.slugs,
    )

    # Linked-source drift revokes trust the audit_module reduction cannot see: a
    # section signed-current whose newest sign-off recorded linked_sources reads
    # UNSIGNED when any linked file no longer matches its recorded hash.
    records = read_decisions(experiment_dir, "notebook", str(audit_id))
    failures: list[tuple[str, str]] = []
    for sect in audit.sections:
        if sect.status not in PASSING_STATUSES:
            failures.append((sect.slug, sect.status))
            continue
        drift = _linked_source_drift(
            experiment_dir, _winning_record(records, sect.slug, sect.signed_section_sha)
        )
        if drift is not None:
            failures.append((sect.slug, f"unsigned (linked-source drift: {drift})"))

    if failures:
        raise errors.SourceUnaudited.for_sections(str(audit_id), failures)
