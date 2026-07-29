"""``notebook-record-receipt`` — journal CODE render receipts, bound to the source.

The emitter's journaling surface for render receipts (notebook-audit T10,
``docs/design/notebook-audit.md``). A render receipt is a CODE attestation that a
section's source was RENDERED (executed) in the caller's environment and whether
its declared assertions errored — the execution evidence the D-attention tier's
assertions-green leg consumes. A ``mutate`` verb: given the audit source ``.py``
and a ``{slug: {output_sha, error}}`` map, it parses the source ON DISK and, for
every entry whose slug exists in the parsed source, journals a
``notebook-render-receipt`` attestation via
:func:`hpc_agent.state.notebook_audit.record_render_receipt`.

**The parse IS the recompute (the load-bearing constraint).** Each receipt is
bound through the ONE attestation kernel against the FRESHLY-PARSED section sha —
never a caller-asserted sha. A receipt can therefore only ever be recorded
against the source as it currently sits on disk, and it reads STALE (greening
nothing) the moment the section drifts. This is what closes the v1 **freshness**
hole: ``notebook-auto-clear`` no longer accepts an opaque *inline* caller receipt;
it reads these journaled, sha-bound receipts and greens only the fresh ones.

**What this does NOT close: truthfulness.** ``output_sha`` and ``error`` are
CALLER-ATTESTED per the D9 execution contract — the verb recomputes FRESHNESS
(the sha bind), not the outcome: it does not execute the source, so an emitter
could journal ``error=False`` without running the assertions. The honest
guarantee is narrower: a receipt vouches only for the exact bytes on disk and
drifts stale when they move; the registration/graduation consumers WEIGH that
caller-attested outcome, they do not re-derive it. The trust boundary is the
emitter (same class as a conforming harness's out-of-band writes), not this
recompute.

Unknown slugs (an entry naming a section the parsed source does not contain — a
stale or mistyped slug) are reported ``skipped``, never fatal: a mismatched entry
must not strand the receipts for the sections that DO exist.

This file lives inside the ``notebook`` subject (beside the pure builder and the
receipt writer it calls), reaching only same-subject ``ops.notebook.*`` and the
``state.*`` substrate — the subject-imports lint is satisfied by construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_record_receipt import (
    NotebookRecordedReceipt,
    NotebookRecordReceiptResult,
    NotebookRecordReceiptSkipped,
    NotebookRecordReceiptSpec,
)
from hpc_agent.state import notebook_audit
from hpc_agent.state.audit_source import parse_percent_source

__all__ = ["notebook_record_receipt"]

_PRIMITIVE = "notebook-record-receipt"

#: The honest reason an entry naming a slug the parsed source lacks is skipped.
_UNKNOWN_SLUG = "unknown-slug"


def _read_source_file(experiment_dir: Path, relpath: str) -> str:
    """Read the caller-declared source ``.py``, or raise SpecInvalid.

    A missing/unreadable file points at a file that is not there — a malformed
    spec, NOT a section that fails to record. Loud, matching the ``notebook-lint``
    / ``notebook-auto-clear`` refusal wording.
    """
    path = Path(relpath)
    if not path.is_absolute():
        path = Path(experiment_dir) / path
    if not path.is_file():
        raise errors.SpecInvalid(f"notebook-record-receipt source file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(
            f"notebook-record-receipt source file could not be read: {path} ({exc})"
        ) from exc


# Dispatched by the merged ``notebook-record`` verb
# (``ops/notebook/record_op.py``) on ``kind: "receipt"``; no longer a
# standalone registration. The seat's semantics (the parse IS the recompute,
# append-only newest-valid-wins, unknown slugs skipped never fatal) are
# unchanged and stay pinned by this module's tests.
def notebook_record_receipt(
    *, experiment_dir: Path, spec: NotebookRecordReceiptSpec
) -> NotebookRecordReceiptResult:
    """Journal a CODE render receipt per known section, bound to the source sha.

    Parses *spec.source* on disk, and for each ``entries`` slug that exists in
    the parsed source, appends a ``notebook-render-receipt`` attestation bound to
    the freshly-recomputed section sha. Entries whose slug is not in the source
    are reported ``skipped`` (``unknown-slug``), never fatal.

    Raises :class:`errors.SpecInvalid` on an unreadable source path or a
    malformed percent-format module (bad/duplicate/misplaced section marker).
    """
    experiment_dir = Path(experiment_dir)
    source = parse_percent_source(_read_source_file(experiment_dir, spec.source))
    by_slug = {sect.slug: sect for sect in source.sections}

    recorded: list[NotebookRecordedReceipt] = []
    skipped: list[NotebookRecordReceiptSkipped] = []
    for slug, entry in spec.entries.items():
        section = by_slug.get(slug)
        if section is None:
            skipped.append(NotebookRecordReceiptSkipped(section=slug, reason=_UNKNOWN_SLUG))
            continue
        # Bind + append. recompute wired to the freshly-parsed sha (the parse IS
        # the recompute — server-computed, never caller-supplied).
        notebook_audit.record_render_receipt(
            experiment_dir,
            audit_id=spec.audit_id,
            section=slug,
            section_sha=section.section_sha,
            recompute=section.section_sha,
            output_sha=entry.output_sha,
            error=entry.error,
        )
        recorded.append(
            NotebookRecordedReceipt(
                section=slug,
                section_sha=section.section_sha,
                output_sha=entry.output_sha,
                error=entry.error,
            )
        )

    return NotebookRecordReceiptResult(audit_id=spec.audit_id, recorded=recorded, skipped=skipped)
