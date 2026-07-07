"""Pydantic models for the ``notebook-auto-clear`` mutate verb (notebook-audit T-auto-clear).

Wire surface over :mod:`hpc_agent.ops.notebook.auto_clear_op` — the CODE-attestor
mirror of the human ``notebook-sign-off``. Given an ``audit_id`` and the audited
source/template ``.py`` (plus the OPAQUE path/import roots the lint needs), it
journals a ``notebook-auto-clear`` attestation
(:func:`hpc_agent.state.notebook_audit.record_auto_clear`) for every section the
D-attention tiering deems ``auto_cleared`` and that is not already cleared at its
current hash — so template-inherited, untouched sections can pass the graduation
gate mechanically, "journaled as auto_cleared with hashes, mechanical, never
claiming human review" (``docs/design/notebook-audit.md`` D-attention).

**Un-fakeability (the load-bearing constraint).** The verb accepts NO
caller-supplied lint findings and NO tier claims — it RECOMPUTES both server-side
(runs the ``notebook-lint`` rules in-process, then :func:`build_audit_view`), so a
caller cannot launder a flagged / modified section into ``auto_cleared`` by
passing empty findings. The only caller inputs are paths / ids / roots.

**The ``receipt`` field was REMOVED (T10, recorded decision).** v1 accepted an
OPAQUE caller-supplied ``receipt`` (``{slug: {output_sha, error}}``) and trusted
it verbatim — the laundering hole: a caller could green an assertion-bearing
section with ``{slug: {error: False}}`` and no execution evidence, no freshness
key. The honest close is to DELETE the trusted input: the mutate verb now reads
JOURNALED render receipts (:func:`~hpc_agent.state.notebook_audit.read_render_receipts`,
sha-fresh entries only), which are code attestations bound to the section sha at
record time (emitted out-of-band by ``notebook-record-receipt``) and therefore
stale-by-construction when the section drifts. The read-only ``notebook-audit-view``
verb keeps its INLINE ``receipt`` for preview because it is read-only and journals
nothing.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NotebookAutoClearSpec(BaseModel):
    """Inputs to ``notebook-auto-clear`` — all paths / ids are caller-declared.

    ``source`` / ``template`` are ``.py`` relpaths under the experiment dir (or
    absolute), parsed by the same percent-format parser. ``input_roots`` /
    ``source_roots`` are the OPAQUE roots the server-side lint recompute needs (a
    missing path literal under ``input_roots`` flags its section, keeping it out
    of the auto-clear) — but for the MUTATE verb they must NOT be supplied by the
    caller (adversarial review F2): the roots are read UNCONDITIONALLY from the
    audit's recorded config (interview.json) so a caller cannot point the lint at
    a directory of planted dummy files and launder a flagged section into
    ``auto_cleared``. Supplying either field is a loud ``SpecInvalid`` refusal.
    (The fields remain on the wire so the refusal is explicit and self-documenting
    rather than a silently-ignored override.) There is deliberately NO ``receipt``
    field (T10): render receipts are read from the journal (sha-fresh only), never
    trusted from the caller — see the module docstring.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-auto-clear input spec")

    audit_id: str = Field(
        min_length=1,
        description=(
            "The notebook decision-journal scope id (filesystem-safe slug) the "
            "auto-clear records are appended to (journal lives at "
            ".hpc/notebooks/<audit_id>.decisions.jsonl). Caller-authored."
        ),
    )
    source: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the audited source .py (jupytext percent "
            "format). Section shas are recomputed fresh; an auto-clear binds the "
            "recomputed sha, never a caller-asserted one."
        ),
    )
    template: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the template .py. A section auto-clears "
            "only when it is byte-identical (inherited) to its template section."
        ),
    )
    input_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Must be EMPTY (adversarial review F2): the mutate verb reads the "
            "data-path roots from the audit's recorded config (interview.json), "
            "never from the caller — supplying non-empty roots is a SpecInvalid "
            "refusal so a section cannot be laundered into auto_cleared with "
            "planted roots."
        ),
    )
    source_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Must be EMPTY (adversarial review F2): import roots are read from the "
            "audit's recorded config, never from the caller — supplying non-empty "
            "roots is a SpecInvalid refusal."
        ),
    )


class NotebookAutoClearedSection(BaseModel):
    """One section a CODE auto-clear record was appended for this call."""

    model_config = ConfigDict(extra="forbid", title="notebook-auto-clear cleared section")

    section: str
    # The recomputed section sha the auto-clear attestation binds.
    section_sha: str
    # The projection sha (what the tiering saw) recorded on the attestation.
    view_sha: str


class NotebookAutoClearSkipped(BaseModel):
    """One section that was NOT auto-cleared, with the honest reason.

    ``reason`` is either a D-attention tier (``human_required`` — a modified /
    lint-flagged / ungreen-assertion section the code may never clear) or
    ``already-current`` (the section is already cleared-current in the journal —
    an auto-clear or a human sign-off at this hash — so a re-run appends nothing).
    """

    model_config = ConfigDict(extra="forbid", title="notebook-auto-clear skipped section")

    section: str
    reason: str


class NotebookAutoClearResult(BaseModel):
    """Honest accounting of one auto-clear pass.

    ``cleared`` names the sections a NEW record was appended for; ``skipped``
    names every other source section with its reason. An idempotent re-run at
    unchanged hashes clears nothing and skips everything ``already-current``.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-auto-clear output data")

    audit_id: str
    cleared: list[NotebookAutoClearedSection] = Field(
        default_factory=list,
        description="Sections a fresh CODE auto-clear was journaled for, in source order.",
    )
    skipped: list[NotebookAutoClearSkipped] = Field(
        default_factory=list,
        description=(
            "Sections NOT cleared, in source order — human_required (never "
            "clearable by code) or already-current (idempotent no-op)."
        ),
    )
