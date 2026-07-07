"""Pydantic wire models for the two render/ingest verbs.

Plugin-local (NOT ``hpc_agent._wire``): a plugin ships its own boundary models.
Each ``@primitive`` carries these as its ``CliShape.spec_model`` so ``--spec``
still model-validates at the CLI seam even though the plugin ships no JSON
schema (``schema_ref=None`` — recorded reason in ``render.py`` / ``ingest.py``:
the Pydantic model is the single source of truth and validates on dispatch;
hand/tool-generated JSON schema would only add a drift surface).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── notebook-render ──────────────────────────────────────────────────────────


class NotebookRenderSpec(BaseModel):
    """Inputs to ``notebook-render``."""

    model_config = ConfigDict(extra="forbid", title="notebook-render input spec")

    audit_id: str = Field(
        min_length=1,
        description=(
            "The notebook decision-journal scope id (filesystem-safe slug). Names "
            "the journal the render reads section status from and, on "
            "--record_receipts, writes receipts to."
        ),
    )
    source: str = Field(
        min_length=1,
        description="Experiment-relative path to the audited source .py (jupytext percent format).",
    )
    template: str = Field(
        min_length=1,
        description="Experiment-relative path to the template .py (slugs = required inventory).",
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Experiment-relative path for the rendered .ipynb. Defaults to "
            "_notebooks/<audit_id>.ipynb."
        ),
    )
    execute: bool = Field(
        default=False,
        description=(
            "Execute the notebook via nbclient in the CURRENT env and compute a "
            "per-section output_sha over the cells' canonicalized outputs."
        ),
    )
    record_receipts: bool = Field(
        default=False,
        description=(
            "Journal a render receipt per section (sha-bound, via the core "
            "notebook-record-receipt op). Requires execute=true."
        ),
    )
    lint_findings: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Opaque lint findings passed through to the core audit view (each may "
            "name its section via slug/section/section_slug). Never interpreted here."
        ),
    )
    attention_order: list[str] | None = Field(
        default=None,
        description="Optional caller slug ordering applied to the core audit view (T12).",
    )


class RenderedSection(BaseModel):
    """One audited section as the render reflected it."""

    model_config = ConfigDict(extra="forbid", title="notebook-render section")

    slug: str
    status: str
    tier: str


class NotebookRenderResult(BaseModel):
    """Result of one render pass."""

    model_config = ConfigDict(extra="forbid", title="notebook-render output data")

    audit_id: str
    output_path: str
    sections: list[RenderedSection] = Field(default_factory=list)
    executed: bool = False
    receipts_recorded: list[str] = Field(
        default_factory=list,
        description="Section slugs a render receipt was journaled for (execute+record_receipts).",
    )
    receipts_skipped: list[str] = Field(
        default_factory=list,
        description="Slugs the core record-receipt op skipped (unknown-slug).",
    )
    canonicalizer: str | None = Field(
        default=None,
        description=(
            "The output canonicalizer that produced each section output_sha "
            "(always 'nbdime' on an executed render; None when not executed). "
            "Core's receipt-entry model forbids extra keys, so this identity is "
            "recorded here and in the notebook metadata, not on the receipt."
        ),
    )
    canonicalizer_version: str | None = Field(
        default=None,
        description=(
            "importlib.metadata version of the canonicalizer (nbdime) bound to "
            "the output_sha values — a version shift reads as an explicit "
            "canonicalizer change, never silent receipt drift. None when not executed."
        ),
    )


# ── notebook-ingest-signoffs ─────────────────────────────────────────────────


class NotebookIngestSignoffsSpec(BaseModel):
    """Inputs to ``notebook-ingest-signoffs``."""

    model_config = ConfigDict(extra="forbid", title="notebook-ingest-signoffs input spec")

    audit_id: str = Field(min_length=1, description="The notebook decision-journal scope id.")
    source: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the CURRENT audited source .py. The "
            "section_sha / view_sha committed with each sign-off are recomputed "
            "from THIS source (never the possibly-stale notebook)."
        ),
    )
    template: str = Field(
        min_length=1,
        description="Experiment-relative path to the template .py (drives the recomputed view).",
    )
    notebook_path: str = Field(
        min_length=1,
        description="Experiment-relative path to the human-edited .ipynb to ingest sign-offs from.",
    )


class IngestedSignoff(BaseModel):
    """One section whose typed sign-off landed through the gate."""

    model_config = ConfigDict(extra="forbid", title="notebook-ingest-signoffs ingested")

    section: str
    section_sha: str
    view_sha: str


class RefusedSignoff(BaseModel):
    """One typed sign-off the gate (or resolution) refused — per-section, non-fatal."""

    model_config = ConfigDict(extra="forbid", title="notebook-ingest-signoffs refused")

    section: str
    reason: str


class NotebookIngestSignoffsResult(BaseModel):
    """Honest accounting of one ingest pass."""

    model_config = ConfigDict(extra="forbid", title="notebook-ingest-signoffs output data")

    audit_id: str
    ingested: list[IngestedSignoff] = Field(default_factory=list)
    refused: list[RefusedSignoff] = Field(default_factory=list)
    skipped_empty: list[str] = Field(
        default_factory=list,
        description="Sign-off cells whose scaffold was left unchanged (no typed text).",
    )
    utterance_log: str = Field(
        default="absent-namespace",
        description=(
            "'written' when typed sign-offs were appended to the out-of-band "
            "utterance log (full-strength tier); 'absent-namespace' when the "
            "journal namespace does not exist so the write no-oped (degraded tier, "
            "reported honestly — no-scaffold discipline)."
        ),
    )
