"""Pydantic models for the ``notebook-status`` query verb (notebook-audit T6).

Wire surface over :mod:`hpc_agent.state.notebook_audit` — the per-section
audit-state reduction. ``notebook-status`` is a PURE READ: it recomputes each
section's current sha from the ``.py`` source on disk, replays the audit_id's
decision journal, and reduces every REQUIRED (template) section to a status in
the T6 vocabulary (``signed_current`` / ``auto_cleared`` / ``signed_stale`` /
``unsigned``) plus the whole-module gate predicate.

Boundary posture: the section slug is a caller-authored id the framework
attaches NO vocabulary to (it is IDENTITY, never a role); a status is a
mechanical reduction of hashes and record order, never a judgement about what a
section MEANS. The models carry slugs, shas, and the closed status vocabulary —
nothing about the experiment's semantics.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NotebookStatusSpec(BaseModel):
    """Inputs to ``notebook-status``.

    All three relpaths / ids are resolved against the ``--experiment-dir``: the
    ``source`` and ``template`` are ``.py`` files in jupytext percent format
    (parsed by the same section parser), and ``audit_id`` names the notebook
    decision-journal scope whose sign-off / auto-clear records are replayed.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-status input spec")

    audit_id: str = Field(
        min_length=1,
        description=(
            "The notebook decision-journal scope id (filesystem-safe slug) whose "
            "sign-off / auto-clear records are reduced. Caller-authored."
        ),
    )
    source: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the audited source .py (jupytext percent "
            "format). Its per-section shas are recomputed fresh on every call."
        ),
    )
    template: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the template .py. Its section slugs are "
            "the REQUIRED inventory the rollup verdict is computed over."
        ),
    )


class NotebookSectionStatus(BaseModel):
    """The reduced audit state of one required section."""

    model_config = ConfigDict(extra="forbid", title="notebook section status")

    slug: str
    # One of signed_current / auto_cleared / signed_stale / unsigned.
    status: str
    # The section's current sha recomputed from the source .py — null when the
    # required template section is absent from the source (nothing to sign).
    current_section_sha: str | None = None
    # The sha the newest valid attestation actually attested — null when unsigned
    # by absence.
    signed_section_sha: str | None = None
    # The projection sha the human saw (view_sha), when recorded.
    view_sha: str | None = None
    # "human" / "code" of the newest valid record, or null when unsigned by
    # absence.
    attestor: str | None = None


class NotebookModuleAttention(BaseModel):
    """ONE attention charge for an UNSIGNED linked src module (wave-3 piece 3).

    Attention is charged per CHANGED PIECE, never per dependent: a src module an
    audited section imports under a ``source_root`` costs the human ONE look when
    its current content is unsigned — not one per dependent section. Signing the
    module (``notebook-module-sign-off``) clears every dependent at once. A SIGNED
    module produces no item.
    """

    model_config = ConfigDict(extra="forbid", title="notebook module attention item")

    module: str
    # The module's experiment-relative POSIX path (its identity for a sign-off).
    file: str
    # The module's current normalized sha (first 12 chars) — what a sign-off binds.
    module_sha12: str
    # The section slugs that import this module (why the attention matters).
    dependents: list[str] = Field(default_factory=list)
    # The sha12 of a PRIOR human module sign-off at a DIFFERENT sha (the
    # "diff vs last-signed" anchor), or null when never signed before.
    last_signed_sha12: str | None = None
    # Moved-code disclosure (piece 5, ADVISORY only — never clears): a HUMAN-signed
    # section whose body this module closely matches, or null.
    moved_from_section: str | None = None
    # (matching, total) normalized-line counts backing moved_from_section, or null.
    moved_overlap: list[int] | None = None


class NotebookStatusResult(BaseModel):
    """Per-section audit statuses + the whole-module gate verdict.

    ``passed`` is the graduation-gate predicate: every required section is
    current (``signed_current`` / ``auto_cleared`` / ``reused``). It is the rollup
    T9 consumes; a false ``passed`` names the drifted/unsigned sections in
    ``sections``.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-status output data")

    audit_id: str
    sections: list[NotebookSectionStatus] = Field(
        default_factory=list,
        description="Per required-section status, in template order.",
    )
    passed: bool = Field(
        description=(
            "True iff every required section is current (signed_current, "
            "auto_cleared, or reused) — the graduation gate's pass predicate."
        ),
    )
    module_attention: list[NotebookModuleAttention] = Field(
        default_factory=list,
        description=(
            "ONE item per UNSIGNED linked src module (never per dependent) — sign "
            "the module to clear all its dependent sections at once. Empty when "
            "every linked module is signed or the audit links none."
        ),
    )
