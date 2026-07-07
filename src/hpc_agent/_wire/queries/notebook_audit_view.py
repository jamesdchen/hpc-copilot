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

    audit_id: str = Field(
        min_length=1,
        description=(
            "The notebook decision-journal scope id (filesystem-safe slug). Names "
            "where the TRUSTED-DISPLAY render files are written: per section, this "
            "verb writes `<experiment>/.hpc/renders/<audit_id>/<slug>.<view_sha12>.md` "
            "— the content-addressed, code-written artifact the T8 sign-off gate "
            "requires (the audit view relayed in chat is model-carried; the render "
            "file on disk is the trusted one)."
        ),
    )
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
    input_roots: list[str] | None = Field(
        default=None,
        description=(
            "Opaque data-path roots for the CANONICAL view's server-side lint "
            "recompute. Default (null) uses the audit's RECORDED roots "
            "(interview.json audited_source) — so the skill's default flow produces "
            "CANONICAL view_shas the T8 gate accepts. Passing roots explicitly is an "
            "OVERRIDE that makes the result a PREVIEW (`canonical: false`)."
        ),
    )
    source_roots: list[str] | None = Field(
        default=None,
        description=(
            "Opaque import roots for the canonical lint recompute. Default (null) "
            "uses the recorded roots; an explicit value is a PREVIEW override."
        ),
    )
    lint_findings: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "OPAQUE lint findings for a PREVIEW build. When NON-EMPTY the view is a "
            "PREVIEW (`canonical: false`) built from these caller-supplied findings "
            "instead of the server-recomputed lint — its view_shas the T8 gate may "
            "refuse. Leave EMPTY (the default) to get the CANONICAL view whose lint "
            "is recomputed server-side from the recorded roots. Each finding names "
            "its section via a `slug` / `section` / `section_slug` key."
        ),
    )
    receipt: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional OPAQUE INLINE execution receipt `{slug: {output_sha, error}}` "
            "for preview only (this read-only verb journals nothing). `error is "
            "False` marks that section's declared assertions GREEN; absent a "
            "receipt, a section WITH assertions is not green (unverified is not "
            "green). An inline entry carries no `section_sha`, so it is not "
            "sha-freshness-gated here — the mutate `notebook-auto-clear` path "
            "reads JOURNALED, sha-bound receipts (`notebook-record-receipt`) "
            "instead, which drift stale by construction."
        ),
    )
    attention_order: list[str] | None = Field(
        default=None,
        description=(
            "Optional caller-supplied section-slug ordering for the presented "
            "sections + markdown (T12). Default (null) is source order. Listed "
            "slugs are shown FIRST in the given order; unknown slugs are ignored; "
            "source slugs the order omits keep source order after the listed ones. "
            "It changes what the human saw, so it participates in the module "
            "view_sha; per-section view_shas are unaffected."
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
    render_path: str = Field(
        description=(
            "Experiment-relative path to the content-addressed TRUSTED-DISPLAY "
            "render file this verb wrote for the section (`.hpc/renders/<audit_id>/"
            "<slug>.<view_sha12>.md`). Where the harness can display files, SEND "
            "this file and let chat carry only the slug + shas; the T8 sign-off gate "
            "requires it to exist and be current at append (the lock is the gate, "
            "not the relay)."
        ),
    )


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
    canonical: bool = Field(
        default=True,
        description=(
            "True when this view was built with the CANONICAL configuration — the "
            "audit's RECORDED lint roots + attention order, the server-recomputed "
            "lint, and the journaled receipts — so its per-section view_shas MATCH "
            "what the T8 sign-off gate recomputes and will accept. False when an "
            "OVERRIDE made it a PREVIEW (explicit roots/order differing from the "
            "recorded config, explicit lint_findings, or an inline receipt): a "
            "preview is for inspection; signing a preview view_sha is refused."
        ),
    )
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
