"""Pydantic models for the ``notebook-audit-view`` query verb (notebook-audit T5-verb).

Wire surface over :mod:`hpc_agent.ops.notebook.audit_view` — the deterministic
D6 *interface* over an audited ``.py``. ``notebook-audit-view`` is a PURE READ:
it parses the source + template (percent format), projects every SOURCE section
(classification-by-hash, diff-from-template, static assertion table, opaque lint
flags, D-attention tier), and returns the canonical per-section + module-level
projection plus its code-rendered ``markdown`` (the verbatim-relay posture — the
skill relays that markdown, never LLM-freeform prose about a section).

Seam posture (recorded per the T5-verb brief): the lint module exposes NO
reusable non-primitive function that computes a lint result — its orchestration
lives entirely inside the ``notebook-lint`` primitive body. So this verb does
NOT recompute findings itself; it accepts ``lint_findings`` as an OPAQUE spec
field the caller chains in (``notebook-lint`` → ``notebook-audit-view``). The
findings are consumed opaquely by :func:`build_audit_view` (only a slug-naming
key attributes a finding to a section) — this wire model attaches no vocabulary
to them.

Boundary posture: a section slug is a caller-authored id the framework attaches
NO vocabulary to (IDENTITY, never a role); a classification / tier is a
mechanical reduction of hashes, flag counts, and static asserts, never a
judgement about what a section MEANS.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NotebookAuditViewSpec(BaseModel):
    """Inputs to ``notebook-audit-view``.

    ``source`` / ``template`` are ``.py`` relpaths resolved against the
    ``--experiment-dir`` (jupytext percent format, parsed by the same section
    parser). ``lint_findings`` is the OPAQUE, caller-chained finding list (from
    ``notebook-lint``); ``receipt`` is the opaque v1.5 execution receipt accepted
    for forward-compat.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-audit-view input spec")

    source: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the audited source .py (jupytext percent "
            "format). Every SOURCE section is projected; per-section shas are "
            "recomputed fresh on every call."
        ),
    )
    template: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the template .py. Each source section is "
            "classified (inherited / added / modified) and diffed against the "
            "template section that shares its slug."
        ),
    )
    lint_findings: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "OPAQUE lint findings, chained in from `notebook-lint` (its `findings` "
            "list). Each finding is embedded verbatim under the section it names "
            "(via a `slug` / `section` / `section_slug` key); a finding with no "
            "such key is module-scoped. Never parsed or interpreted here. A section "
            "with zero flags is one auto-clear precondition."
        ),
    )
    receipt: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional OPAQUE execution receipt `{slug: {output_sha, error}}` "
            "(v1.5 forward-compat). `error is False` marks that section's declared "
            "assertions GREEN; absent a receipt, a section WITH assertions is not "
            "green (unverified is not green)."
        ),
    )


class NotebookViewAssertion(BaseModel):
    """One statically-discovered ``assert`` in a section (never executed)."""

    model_config = ConfigDict(extra="forbid", title="notebook-audit-view assertion")

    test: str
    # 1-based line WITHIN the section source (relative, never an absolute path).
    lineno: int
    msg: str | None = None


class NotebookSectionView(BaseModel):
    """The deterministic projection of one SOURCE section (the primary object).

    ``view_sha`` is the per-section sha a sign-off binds (D5). ``diff`` is the
    stdlib unified diff of the normalized template → source section (empty ⇔
    ``inherited``). ``lint_flags`` embeds the caller's findings for this section
    OPAQUELY.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-audit-view section")

    slug: str
    # One of inherited / added / modified (classification-by-hash, D6).
    classification: str
    # One of auto_cleared / human_required (D-attention tier).
    tier: str
    section_sha: str
    # The template section sha this slug matched, or null when added.
    template_section_sha: str | None = None
    diff: list[str] = Field(default_factory=list)
    assertions: list[NotebookViewAssertion] = Field(default_factory=list)
    # Opaque findings attributed to this section, in caller order.
    lint_flags: list[dict[str, Any]] = Field(default_factory=list)
    view_sha: str


class NotebookAuditViewResult(BaseModel):
    """The whole-source audit view: per-section projections + a module roll-up.

    ``markdown`` is the code-rendered human projection (the verbatim-relay
    posture — the skill relays it as-is; NO LLM-freeform prose enters the audit
    path). ``view_sha`` is the deterministic roll-up over the section shas and
    the two module fingerprints; any section OR preamble edit moves it.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-audit-view output data")

    sections: list[NotebookSectionView] = Field(
        default_factory=list,
        description="One projection per SOURCE section, in source order.",
    )
    dropped_template_slugs: list[str] = Field(
        default_factory=list,
        description=(
            "Template slugs absent from the source (a section the template declared "
            "but the draft dropped) — surfaced, never silently hidden. The "
            "graduation gate refuses on these; the view only shows them."
        ),
    )
    source_module_sha: str
    template_module_sha: str
    view_sha: str = Field(
        description=(
            "Deterministic roll-up sha over the section shas + the two module "
            "fingerprints; any section or preamble edit moves it."
        ),
    )
    markdown: str = Field(
        description=(
            "The code-rendered markdown projection of the same fields, for VERBATIM "
            "relay. Same view → byte-identical markdown."
        ),
    )
