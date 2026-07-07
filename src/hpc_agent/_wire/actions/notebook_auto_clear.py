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
passing empty findings. The only caller inputs are paths / ids / roots and an
OPAQUE forward-compat receipt.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NotebookAutoClearSpec(BaseModel):
    """Inputs to ``notebook-auto-clear`` — all paths / ids are caller-declared.

    ``source`` / ``template`` are ``.py`` relpaths under the experiment dir (or
    absolute), parsed by the same percent-format parser. ``input_roots`` /
    ``source_roots`` are the OPAQUE roots the server-side lint recompute needs (a
    missing path literal under ``input_roots`` flags its section, keeping it out
    of the auto-clear). ``receipt`` is the OPAQUE v1.5 execution receipt.
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
            "OPAQUE data-path roots the server-side lint recompute tests path "
            "literals against. A section with a missing literal is flagged and "
            "therefore NOT auto-cleared."
        ),
    )
    source_roots: list[str] = Field(
        default_factory=list,
        description="OPAQUE import roots the server-side lint recompute resolves imports under.",
    )
    receipt: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional OPAQUE execution receipt `{slug: {output_sha, error}}` "
            "(v1.5 forward-compat). `error is False` greens that section's declared "
            "assertions; absent a receipt, a section WITH assertions is not green "
            "and stays human_required."
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
