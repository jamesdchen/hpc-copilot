"""Domain-pack bind currency + per-slot receipt reduction (T2).

Design origin: ``docs/design/domain-packs.md`` ("The bind event", "Re-bind =
drift", "Receipt naming + the gate contract", "Currency semantics — the
notebook_audit reduction reused, one definition"). This module answers two
questions over a pack's decision journal, both routed through the ONE
attestation kernel (``state/attestation.py``) so the drift-revocation logic is
never re-inlined:

* :func:`current_bind` — *what standards are in force right now?* The
  newest-valid ``pack-bind`` projection. A re-bind at a new manifest sha makes
  the old bind STALE by construction (``attestation.reduce``), so the newest
  valid bind IS the current one.
* :func:`slot_status` — *is this receipt slot cleared, under the current
  standards, against the current bytes on disk?* A slot's ``pack-receipt``
  records reduce through the kernel with ``current_sha`` **recomputed from disk
  at read time**: a receipt is CURRENT iff the current bind's manifest sha AND
  every checked file's on-disk sha still hash to the recorded ``content_sha``.
  Stale receipt = missing receipt (drift = unsigned by construction; a stale
  CODE record has no human to inform — the notebook-audit stale-auto-clear
  ruling, reused).

This is the read side. The record side (``pack-record-receipt``, T5) computes
the SAME ``content_sha`` form — :func:`receipt_content_sha` is the one
definition both use (the notebook "the parse IS the recompute" precedent).

**No journal I/O of its own.** Every reader takes a *records* list (the pack
journal in append order, newest last). The pack ``"pack"`` scope kind and its
``.hpc/packs/<name>.decisions.jsonl`` path branch land in T8 (Wave C); once they
do, callers (``pack-status`` T6, the gate T9) read via
``read_decisions(experiment_dir, "pack", pack_name)`` and pass the list here.
Keeping the record list at the boundary makes this module importable and
testable standalone, ahead of T8.

Pure-ish: reads pack FILES off disk to recompute their raw-bytes shas (the
currency recompute), but holds no ``_wire`` import, no SSH, no scheduler.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent.state import attestation

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from hpc_agent.state.attestation import Attestation

__all__ = [
    "PACK_BIND_BLOCK",
    "PACK_RECEIPT_BLOCK",
    "PACK_SUBJECT_KIND",
    "PACK_RECEIPT_SUBJECT_KIND",
    "CURRENT_PASSED",
    "CURRENT_FAILED",
    "STALE",
    "MISSING",
    "SLOT_STATUSES",
    "PASSING_STATUSES",
    "PackBind",
    "SlotStatus",
    "current_bind",
    "slot_status",
    "slot_statuses",
    "receipt_content_sha",
]

#: The bind record's block — a ``pack-bind`` decision-journal record projects to
#: a CODE attestation whose ``content_sha`` is the manifest sha.
PACK_BIND_BLOCK = "pack-bind"

#: The receipt record's block — a ``pack-receipt`` record projects to a CODE
#: attestation whose ``content_sha`` is the composite :func:`receipt_content_sha`.
PACK_RECEIPT_BLOCK = "pack-receipt"

#: The opaque attestation ``subject_kind`` a bind rides. ``subject_id`` is the
#: pack name. The kernel never interprets it.
PACK_SUBJECT_KIND = "pack"

#: The opaque attestation ``subject_kind`` a receipt rides. ``subject_id`` is the
#: caller-authored slot slug (DP4). The kernel never interprets it.
PACK_RECEIPT_SUBJECT_KIND = "pack-receipt-slot"

# --- the per-slot status vocabulary -----------------------------------------
#: Current bind + current bytes + the receipt reported ``passed=true``. The ONLY
#: status the gate (T9) accepts.
CURRENT_PASSED = "current+passed"

#: Current bind + current bytes, but the receipt reported ``passed=false`` — the
#: check ran against live content and failed. A loud, current negative.
CURRENT_FAILED = "current+failed"

#: The newest receipt matched an OLDER composite sha: the bind was re-bound, or a
#: checked file drifted on disk. Drift = unsigned by construction (a stale CODE
#: receipt has no human to inform — the notebook stale-auto-clear ruling reused).
STALE = "stale"

#: No valid receipt for the slot at all.
MISSING = "missing"

#: Every status a slot reduction can yield.
SLOT_STATUSES = frozenset({CURRENT_PASSED, CURRENT_FAILED, STALE, MISSING})

#: The statuses that PASS the gate. Only a current, passing receipt clears a slot.
PASSING_STATUSES = frozenset({CURRENT_PASSED})


@dataclass(frozen=True)
class PackBind:
    """The current-in-force bind: which pack, version, and standards (by sha).

    * ``pack`` — the pack name (the attestation ``subject_id``).
    * ``version`` — the opaque version string core echoes, never compares.
    * ``manifest_sha`` — the bind's ``content_sha``; the identity of the
      standards in force. A re-bind moves it and revokes everything signed under
      the old one.
    * ``files`` — the manifest's ``[{path, sha256}, …]`` list, verbatim (opaque
      to the kernel; carried for the gate's on-disk drift check and the dossier).
    * ``seams`` — the seam-name list the bind recorded.
    """

    pack: str
    version: str | None
    manifest_sha: str
    files: tuple[dict[str, Any], ...]
    seams: tuple[str, ...]


@dataclass(frozen=True)
class SlotStatus:
    """The reduced clearance state of one caller-authored receipt slot.

    Shaped so T6's ``pack-status`` and T9's gate consume it directly: the gate
    passes iff :attr:`passing`, and :attr:`reason` names why a non-passing slot
    failed (``missing`` / ``stale`` / ``failed``) for the refusal message.

    * ``slot`` — the caller-authored slot slug (the attestation ``subject_id``).
    * ``status`` — one of :data:`SLOT_STATUSES`.
    * ``passed`` — the ``passed`` boolean the newest valid receipt recorded, or
      ``None`` when the slot is :data:`MISSING`.
    * ``reason`` — ``None`` when :attr:`passing`; else the failing reason word
      (``"missing"`` / ``"stale"`` / ``"failed"``) the gate names.
    * ``manifest_sha`` — the current bind's manifest sha the reduction was run
      against, or ``None`` when there is no current bind.
    * ``checked`` — the checked relpaths of the newest valid receipt (empty when
      missing) — what the currency recompute hashed on disk.
    """

    slot: str
    status: str
    passed: bool | None
    reason: str | None
    manifest_sha: str | None
    checked: tuple[str, ...]

    @property
    def passing(self) -> bool:
        """True iff this slot clears the gate (current bind, current bytes, passed)."""
        return self.status in PASSING_STATUSES


def receipt_content_sha(manifest_sha: str, checked: Mapping[str, str]) -> str:
    """The composite ``content_sha`` a receipt binds on — the ONE definition.

    ``sha256`` over the canonical-JSON form
    ``{"manifest_sha": …, "checked": {relpath: sha, …}}`` (the normative
    ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
    form — ``docs/internals/harness-contract.md``). The record side
    (``pack-record-receipt``, T5) builds this from the on-disk shas at RECORD
    time; :func:`slot_status` rebuilds the IDENTICAL form from the on-disk shas
    at READ time. Byte-equal inputs → byte-equal sha → the receipt reads CURRENT;
    anything moved → a different sha → STALE (the currency semantics). No new
    canonicalization is invented — this is the shared definition both sides
    import, so record-form and read-form can never drift apart.
    """
    payload = {"manifest_sha": manifest_sha, "checked": dict(checked)}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """Raw-bytes SHA-256 hex of *path*, or ``""`` when it cannot be read.

    Pack files hash as RAW BYTES (the dossier manifest-entry form; ``normalize_
    source`` does NOT apply — pack files are not necessarily Python). A missing
    or unreadable checked file yields ``""``, so its slot's currency recompute
    produces a different composite sha and the receipt reads STALE — a deleted
    file is drift, never a silent pass.

    # T1 seam: ``state/pack.py`` (parallel, T1) owns the canonical raw-bytes sha
    # helper; the orchestrator should unify this with it. Kept private + minimal
    # so this module imports standalone ahead of T1.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _project_bind(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a journal record to a bind attestation dict, or ``None``.

    ``None`` for any block other than :data:`PACK_BIND_BLOCK`. A recognised block
    with a malformed ``resolved`` still projects; the kernel's ``validate`` then
    refuses it (empty ``subject_id``/``content_sha``) and the reducer skips it.
    The manifest fields ride opaque ``evidence`` (never interpreted by the
    kernel) so the winning projection can rebuild a :class:`PackBind`.
    """
    if record.get("block") != PACK_BIND_BLOCK:
        return None
    resolved = record.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    return {
        "attestor": "code",
        "subject_kind": PACK_SUBJECT_KIND,
        "subject_id": resolved.get("pack"),
        "content_sha": resolved.get("manifest_sha"),
        "evidence": {
            "version": resolved.get("version"),
            "files": resolved.get("files"),
            "seams": resolved.get("seams"),
        },
    }


def _project_receipt(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a journal record to a receipt attestation dict, or ``None``.

    ``None`` for any block other than :data:`PACK_RECEIPT_BLOCK`. ``content_sha``
    is the composite the record was BOUND at (recorded by T5); ``checked`` and
    ``passed`` ride opaque ``evidence`` so the read side can recompute the
    currency sha and map the verdict. A malformed ``resolved`` projects to a
    record the kernel then refuses (empty ``subject_id``/``content_sha``).
    """
    if record.get("block") != PACK_RECEIPT_BLOCK:
        return None
    resolved = record.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    return {
        "attestor": "code",
        "subject_kind": PACK_RECEIPT_SUBJECT_KIND,
        "subject_id": resolved.get("slot"),
        "content_sha": resolved.get("content_sha"),
        "evidence": {
            "passed": resolved.get("passed"),
            "checked": resolved.get("checked"),
            "manifest_sha": resolved.get("manifest_sha"),
        },
    }


def _newest_valid(
    projected: Sequence[dict[str, Any]], subject_id: str | None = None
) -> Attestation | None:
    """The newest VALID attestation in *projected* (optionally filtered by subject).

    Selection only — the ``current``/``stale``/``absent`` DRIFT decision is
    :func:`~hpc_agent.state.attestation.reduce`'s job and is NOT reproduced here
    (this never compares a ``content_sha`` to a current sha; it reads the winning
    record's attested sha + opaque evidence, which the kernel's verdict does not
    surface). Append order → the last valid match is the newest (the kernel's own
    precedence). Malformed records are skipped, never raised — one bad line never
    strands the rest of the audit trail. This mirrors
    ``state/notebook_audit.py::_newest_valid`` exactly.
    """
    from hpc_agent import errors

    newest: Attestation | None = None
    for record in projected:
        try:
            att = attestation.validate(record)
        except errors.SpecInvalid:
            continue
        if subject_id is not None and att.subject_id != subject_id:
            continue
        newest = att
    return newest


def current_bind(
    records: Sequence[Mapping[str, Any]], *, pack: str | None = None
) -> PackBind | None:
    """The bind currently in force over *records*, or ``None`` when never bound.

    The newest-valid ``pack-bind`` projection. A pack journal holds one pack, so
    the newest valid bind is the current one; a re-bind at a new manifest sha
    makes the older one STALE. When *pack* is given, only that pack's binds are
    considered (defensive — a per-pack journal already isolates them).

    Routes the currency verdict through the ONE kernel
    (:func:`~hpc_agent.state.attestation.reduce`), never a re-inlined
    newest-first: the newest valid bind is confirmed CURRENT against its OWN
    manifest sha, and any older bind at a different sha reduces STALE — the
    "re-bind = drift" property, expressed through the kernel rather than
    re-implemented here.
    """
    projected = [p for r in records if (p := _project_bind(r)) is not None]
    newest = _newest_valid(projected, subject_id=pack)
    if newest is None:
        return None
    # Route the drift verdict through the ONE kernel (never re-inlined). The
    # newest valid bind defines the current sha, so it reduces CURRENT and any
    # older bind at a different manifest sha reduces STALE.
    verdict = attestation.reduce(
        projected, current_sha=newest.content_sha, subject_id=newest.subject_id
    )
    if verdict != attestation.CURRENT:  # defensive — unreachable given newest defines it
        return None
    evidence = newest.evidence if isinstance(newest.evidence, dict) else {}
    files_raw = evidence.get("files")
    files = (
        tuple(f for f in files_raw if isinstance(f, dict)) if isinstance(files_raw, list) else ()
    )
    seams_raw = evidence.get("seams")
    seams = tuple(s for s in seams_raw if isinstance(s, str)) if isinstance(seams_raw, list) else ()
    version = evidence.get("version")
    return PackBind(
        pack=newest.subject_id,
        version=version if isinstance(version, str) else None,
        manifest_sha=newest.content_sha,
        files=files,
        seams=seams,
    )


def slot_status(
    records: Sequence[Mapping[str, Any]],
    *,
    experiment_dir: Path,
    slot: str,
    bind: PackBind | None = None,
) -> SlotStatus:
    """Reduce one slot's ``pack-receipt`` records to a :class:`SlotStatus`.

    *records* are the whole pack journal (append order, newest last).
    *experiment_dir* anchors the on-disk sha recomputes (checked relpaths resolve
    against it, the ``_AuditedSource.source`` precedent). *bind* is the current
    bind; when omitted it is computed from *records* — pass it when reducing many
    slots to avoid recomputing it per slot.

    Currency (the notebook_audit reduction reused, one definition): the newest
    valid receipt is CURRENT iff the current bind's manifest sha AND every checked
    file's on-disk sha still hash to the recorded composite ``content_sha``. The
    drift verdict routes through the ONE kernel
    (:func:`~hpc_agent.state.attestation.reduce`) with ``current_sha``
    recomputed from disk here — never a re-inlined sha compare. The verdict maps
    onto the slot vocabulary:

    * ``current`` + ``passed`` → :data:`CURRENT_PASSED`
    * ``current`` + not passed → :data:`CURRENT_FAILED`
    * ``stale`` / no current bind → :data:`STALE`
    * no valid receipt → :data:`MISSING`
    """
    if bind is None:
        bind = current_bind(records)
    projected = [p for r in records if (p := _project_receipt(r)) is not None]
    newest = _newest_valid(projected, subject_id=slot)
    if newest is None:
        return SlotStatus(
            slot=slot,
            status=MISSING,
            passed=None,
            reason="missing",
            manifest_sha=bind.manifest_sha if bind else None,
            checked=(),
        )
    evidence = newest.evidence if isinstance(newest.evidence, dict) else {}
    checked_raw = evidence.get("checked")
    checked = (
        tuple(c for c in checked_raw if isinstance(c, str)) if isinstance(checked_raw, list) else ()
    )
    passed = bool(evidence.get("passed"))
    if bind is None:
        # No current bind → currency cannot be established → the receipt is
        # revoked (a receipt is only current relative to a live bind).
        return SlotStatus(slot, STALE, passed, "stale", None, checked)

    # Recompute the read-form composite sha from CURRENT disk + CURRENT bind, the
    # identical form the record side built (:func:`receipt_content_sha`).
    on_disk = {rel: _file_sha256(experiment_dir / rel) for rel in checked}
    current_sha = receipt_content_sha(bind.manifest_sha, on_disk)
    # Route the drift verdict through the ONE kernel (never re-inlined).
    verdict = attestation.reduce(projected, current_sha=current_sha, subject_id=slot)
    if verdict == attestation.CURRENT:
        if passed:
            return SlotStatus(slot, CURRENT_PASSED, True, None, bind.manifest_sha, checked)
        return SlotStatus(slot, CURRENT_FAILED, False, "failed", bind.manifest_sha, checked)
    # STALE (ABSENT unreachable — newest is not None here). Drift = unsigned.
    return SlotStatus(slot, STALE, passed, "stale", bind.manifest_sha, checked)


def slot_statuses(
    records: Sequence[Mapping[str, Any]],
    *,
    experiment_dir: Path,
    slots: Sequence[str],
) -> dict[str, SlotStatus]:
    """Reduce many slots at once, sharing one :func:`current_bind` computation.

    Returns ``{slot: SlotStatus}`` for every slug in *slots* (order-independent).
    The convenience T6's ``pack-status`` and T9's gate use to report/verify a
    whole ``receipt_bindings`` list in one pass.
    """
    bind = current_bind(records)
    return {
        slot: slot_status(records, experiment_dir=experiment_dir, slot=slot, bind=bind)
        for slot in slots
    }
