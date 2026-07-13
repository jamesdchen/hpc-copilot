"""Pydantic models for the ``notebook-record-receipt`` mutate verb (notebook-audit T10).

Wire surface over :mod:`hpc_agent.ops.notebook.record_receipt_op` — the emitter's
journaling surface for CODE render receipts. A render receipt asserts that a
section's source was RENDERED (executed) in the caller's environment and whether
its declared assertions errored; it is the execution evidence the D-attention
tier's assertions-green leg consumes (``docs/design/notebook-audit.md`` T10).

**Why a verb (the CLI-verbs doctrine).** The receipt writer
(:func:`hpc_agent.state.notebook_audit.record_render_receipt`) is a state helper;
without an agent-facing verb, an in-session emitter would reach for a bespoke
``python -c`` to journal a receipt. This verb IS that surface: given the audit
source ``.py`` and a ``{slug: {output_sha, error}}`` map, it parses the source ON
DISK and journals one receipt per known slug, bound to the FRESHLY PARSED section
sha (the parse IS the recompute — a receipt can only ever be recorded against
current source). Unknown slugs are reported skipped, never fatal.

Freshness by construction: because each receipt is bound to the section sha it
was recorded at, it reads STALE (and greens nothing) the moment the section
drifts — the property that closes the v1 laundering hole, where
``notebook-auto-clear`` trusted an opaque caller receipt with no execution
evidence and no freshness key.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NotebookReceiptEntry(BaseModel):
    """One section's render outcome — the opaque evidence a receipt carries.

    ``output_sha`` is the caller's hash of the section's rendered output (opaque
    to core — never parsed); ``error`` is whether the render/assertions errored.
    ``error is False`` is what greens the assertions-green tier leg (while the
    receipt is fresh).
    """

    model_config = ConfigDict(extra="forbid", title="notebook-record-receipt entry")

    output_sha: str = Field(
        min_length=1,
        description="OPAQUE caller hash of the section's rendered output. Never parsed by core.",
    )
    error: bool = Field(
        description=(
            "Whether the render / declared assertions errored. `false` greens the "
            "section's assertions-green tier leg (while the receipt is sha-fresh); "
            "`true` never greens."
        ),
    )


class NotebookRecordReceiptSpec(BaseModel):
    """Inputs to ``notebook-record-receipt``.

    ``audit_id`` is the notebook decision-journal scope; ``source`` is the audited
    source ``.py`` relpath (parsed on disk — the parse IS the recompute the
    receipt binds against); ``entries`` maps each rendered section's slug to its
    outcome. A slug absent from the parsed source is reported skipped, not fatal.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-record-receipt input spec")

    audit_id: str = Field(
        min_length=1,
        description=(
            "The notebook decision-journal scope id (filesystem-safe slug) the "
            "receipts are appended to (journal at .hpc/notebooks/<audit_id>.decisions.jsonl). "
            "Caller-authored."
        ),
    )
    source: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the audited source .py (jupytext percent "
            "format). Parsed on disk; each receipt binds the FRESHLY PARSED section "
            "sha — a receipt can only be recorded against current source."
        ),
    )
    entries: dict[str, NotebookReceiptEntry] = Field(
        min_length=1,
        description=(
            "Map of section slug -> render outcome {output_sha, error}. One receipt "
            "is journaled per slug that exists in the parsed source; unknown slugs "
            "are reported skipped."
        ),
    )


class NotebookRecordedReceipt(BaseModel):
    """One section a render receipt was journaled for this call."""

    model_config = ConfigDict(extra="forbid", title="notebook-record-receipt recorded section")

    section: str
    # The freshly-parsed section sha the receipt attestation binds.
    section_sha: str
    output_sha: str
    error: bool


class NotebookRecordReceiptSkipped(BaseModel):
    """One entry that was NOT journaled, with the honest reason.

    ``reason`` is ``unknown-slug`` — the entry named a slug the parsed source
    does not contain (a stale or mistyped slug), so there is no section sha to
    bind a receipt against. Reported, never fatal.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-record-receipt skipped entry")

    section: str
    reason: str


class NotebookRecordReceiptResult(BaseModel):
    """Honest accounting of one record-receipt pass.

    ``recorded`` names the sections a receipt was journaled for; ``skipped`` names
    every entry whose slug was not found in the parsed source.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-record-receipt output data")

    audit_id: str
    recorded: list[NotebookRecordedReceipt] = Field(
        default_factory=list,
        description="Sections a render receipt was journaled for, in entry order.",
    )
    skipped: list[NotebookRecordReceiptSkipped] = Field(
        default_factory=list,
        description="Entries NOT journaled — unknown-slug (no such section in source).",
    )
