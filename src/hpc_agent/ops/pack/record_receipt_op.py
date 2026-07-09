"""``pack-record-receipt`` — journal a CODE domain-check receipt, sha-bound.

The record side of the domain-pack receipt gate (``docs/design/domain-packs.md``,
"Receipt naming + the gate contract"). A pack RECEIPT is a CODE attestation that
a domain check (run entirely OUTSIDE core, DP2) reported ``passed`` for one
caller-authored SLOT, against a set of checked files, under a pack's CURRENT
bind. A ``mutate`` verb: given ``{pack, slot, checked, passed, evidence}`` it
recomputes ON DISK the sha of every checked file AND the current bind's manifest
sha, builds the composite ``content_sha`` **server-side**, binds it through the
ONE attestation kernel, and appends a ``pack-receipt`` record to the pack's
journal.

**The parse IS the recompute (the load-bearing constraint).** Every sha is
recomputed here from the bytes on disk — no wire field lets a caller assert a
``content_sha`` / ``manifest_sha`` / per-file sha the verb then trusts (the
enforcement-map "receipt shas are server-computed" row; the wire-schema pin in
``tests/_wire/test_pack_wire.py`` holds the field set). The composite is built
from :func:`hpc_agent.state.pack_receipts.receipt_content_sha` — the ONE
definition the read side (:func:`~hpc_agent.state.pack_receipts.slot_status`)
rebuilds from disk at gate time, so record-form and read-form can never drift
apart. A caller therefore cannot assert a receipt for content not on disk, and
the receipt reads STALE the instant any checked file (or the bind) moves.

**What this does NOT close: truthfulness.** ``passed`` and ``evidence`` are
CALLER-ATTESTED — the verb recomputes FRESHNESS (the sha bind), not the check's
correctness: it does not run the domain check. The honest guarantee is narrower:
a receipt vouches for the exact bytes on disk under the exact bind, and drifts
stale when they move. The gate WEIGHS the caller-attested ``passed`` (fresh +
``passed=true`` clears the slot); it never re-derives it. The trust boundary is
the emitter (the pack's own CI, per Q4), not this recompute.

**The D7/opted-in split, applied to a mutate verb.** Unlike a gate, this verb is
always invoked EXPLICITLY by a caller naming a pack — the call IS the opt-in, so
the "absence = silence" leg of D7 never arises here. A named pack with no current
bind is therefore always the LOUD (dangling-reference) leg: recording a receipt
against a pack that was never bound (or whose bind is not resolvable) raises
:class:`errors.SpecInvalid`, never a silent no-op — a silent pass on a dangling
reference is the uninstall-softens-gates laundering channel one layer up. The
SLOT is caller-authored (DP4) and needs no membership check: any well-formed slug
records against the current bind (``fills_slots`` is advisory identity only, read
by ``pack-status``, never a record-time gate). A missing checked file is likewise
loud at record time (the bind lock refuses a receipt claiming a file that is not
there); on the READ side a vanished file merely reads stale.

This file lives inside the ``pack`` subject, reaching only the ``state.*``
substrate (``state.pack``, ``state.pack_receipts``, ``state.attestation``, the
decision-journal writer) — the subject-imports lint is satisfied by construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.pack_record_receipt import (
    PackRecordReceiptResult,
    PackRecordReceiptSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state import attestation
from hpc_agent.state.decision_journal import append_decision, read_decisions
from hpc_agent.state.pack import sha256_file
from hpc_agent.state.pack_receipts import (
    PACK_RECEIPT_BLOCK,
    PACK_RECEIPT_SUBJECT_KIND,
    current_bind,
    receipt_content_sha,
)

__all__ = ["pack_record_receipt"]

_PRIMITIVE = "pack-record-receipt"

#: The pack journal's scope kind. T8 (Wave C) landed the ``"pack"``
#: decision-journal scope + its ``.hpc/packs/<name>.decisions.jsonl`` path branch
#: on ``state/decision_journal.py::SCOPE_KINDS``, so a live call now resolves the
#: real journal path (no monkeypatch needed). Read/append route through the ONE
#: decision-journal writer with this scope kind; journal I/O is never
#: re-implemented here (parallel bind_op posture).
_PACK_SCOPE = "pack"

#: The honest mechanical response — never a human-ack token (the
#: ``record_auto_clear`` / render-receipt naming discipline: a CODE record must
#: not read as a human's approval when the journal is replayed or exported).
_RESPONSE = "checked"


def _on_disk_shas(experiment_dir: Path, checked: list[str]) -> dict[str, str]:
    """Recompute each checked file's raw-bytes sha from disk, or raise SpecInvalid.

    A missing/unreadable checked file is a malformed record request — a receipt
    claiming a file that is not there — not a section that silently fails: loud
    :class:`errors.SpecInvalid` naming the path (the record-time bind lock; the
    READ side tolerates a vanished file as drift → stale, but a record must not
    manufacture a sha for absent content).
    """
    out: dict[str, str] = {}
    for rel in checked:
        path = experiment_dir / rel
        try:
            out[rel] = sha256_file(path)
        except OSError as exc:
            raise errors.SpecInvalid(
                f"pack-record-receipt checked file not found or unreadable: {path} "
                f"({exc}) — a receipt cannot be recorded for content that is not on disk"
            ) from exc
    return out


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/packs/<pack>.decisions.jsonl"),
    ],
    error_codes=[errors.SpecInvalid],
    # Append-only: each call journals a fresh receipt line for the slot. A
    # re-record at unchanged shas appends a new line (the newest valid receipt
    # wins on read), so retries are safe but not byte-idempotent — like
    # append-decision and the render receipt.
    idempotent=False,
    cli=CliShape(
        help=(
            "Journal a CODE receipt that a domain check reported passed for a "
            "caller-authored slot, against a set of checked files, under a pack's "
            "current bind. Recomputes every checked file's sha AND the bind's "
            "manifest sha ON DISK and builds the composite content_sha server-side "
            "(no caller-suppliable sha), then binds it through the one attestation "
            "kernel and appends a pack-receipt record. The receipt reads stale the "
            "instant any checked file or the bind moves; passed/evidence stay "
            "caller-attested (weighed by the gate, never re-derived). No current "
            "bind for the named pack is a loud dangling reference. Pure local read "
            "+ journal append, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=PackRecordReceiptSpec,
        schema_ref=SchemaRef(input="pack_record_receipt"),
    ),
    agent_facing=True,
)
def pack_record_receipt(
    *, experiment_dir: Path, spec: PackRecordReceiptSpec
) -> PackRecordReceiptResult:
    """Journal a sha-bound CODE receipt for one slot under the pack's current bind.

    Resolves the pack's current bind (no current bind → loud
    :class:`errors.SpecInvalid`, an opted-in dangling reference), recomputes the
    composite ``content_sha`` from the on-disk checked shas + the bind's manifest
    sha, binds it through :func:`hpc_agent.state.attestation.bind`, and appends a
    ``pack-receipt`` record to the pack's journal. Returns the
    :class:`PackRecordReceiptResult` echo (every sha server-recomputed).

    Raises :class:`errors.SpecInvalid` on no current bind, or a missing/unreadable
    checked file.
    """
    experiment_dir = Path(experiment_dir)

    # T8 seam: read the pack's journal via the ONE writer's read side.
    records = read_decisions(experiment_dir, _PACK_SCOPE, spec.pack)
    bind = current_bind(records, pack=spec.pack)
    if bind is None:
        raise errors.SpecInvalid(
            f"pack-record-receipt: pack {spec.pack!r} has no current bind — a receipt "
            "cannot be recorded against a pack that was never bound (or whose bind is "
            "unresolvable). Bind the pack first; recording against a dangling "
            "reference is a broken setup, not a silent pass."
        )

    # Server-side recompute (the parse IS the recompute): the on-disk sha of every
    # checked file + the current bind's manifest sha → the composite content_sha,
    # via the ONE shared definition the read side rebuilds. Never caller-asserted.
    checked_shas = _on_disk_shas(experiment_dir, spec.checked)
    content_sha = receipt_content_sha(bind.manifest_sha, checked_shas)

    resolved: dict[str, object] = {
        "pack": spec.pack,
        "version": bind.version,
        "manifest_sha": bind.manifest_sha,
        "slot": spec.slot,
        "checked": list(spec.checked),
        "passed": spec.passed,
        "evidence": spec.evidence,
        "content_sha": content_sha,
        "attestor": "code",
    }

    # Route through the ONE attestation kernel (never re-inlined): the receipt is
    # bound on the server-computed composite sha, so it is stale-by-construction
    # when any covered byte moves and can only be recorded against current content.
    projected = {
        "attestor": "code",
        "subject_kind": PACK_RECEIPT_SUBJECT_KIND,
        "subject_id": spec.slot,
        "content_sha": content_sha,
        "evidence": {
            "passed": spec.passed,
            "checked": list(spec.checked),
            "manifest_sha": bind.manifest_sha,
        },
    }
    attestation.bind(projected, recompute=content_sha)

    append_decision(
        experiment_dir,
        scope_kind=_PACK_SCOPE,
        scope_id=spec.pack,
        block=PACK_RECEIPT_BLOCK,
        response=_RESPONSE,
        resolved=resolved,
    )

    return PackRecordReceiptResult(
        pack=spec.pack,
        version=bind.version or "",
        manifest_sha=bind.manifest_sha,
        slot=spec.slot,
        content_sha=content_sha,
        passed=spec.passed,
    )
