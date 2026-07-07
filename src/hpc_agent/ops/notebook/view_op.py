"""``notebook-audit-view`` — the D6 audit VIEW as a read-only query verb.

The verb wrapper over the pure :mod:`hpc_agent.ops.notebook.audit_view` builder
(notebook-audit T5). Given an experiment dir, a source ``.py`` relpath, a
template ``.py`` relpath, opaque chained ``lint_findings``, and an optional
``receipt``, it:

1. reads + parses the source and template as jupytext percent-format modules
   (:func:`hpc_agent.state.audit_source.parse_percent_source`);
2. builds the deterministic :class:`~hpc_agent.ops.notebook.audit_view.AuditView`
   — per-section classification-by-hash, diff-from-template, static assertion
   table, opaque lint flags, and D-attention tier;
3. returns the canonical per-section + module-level projection plus its
   code-rendered ``markdown`` (the verbatim-relay projection the skill hands the
   human unchanged).

Pure local read — no SSH, no scheduler, no write. Derived state, recomputed from
the ``.py`` on disk on every call, so it can never drift from a second source of
truth.

Seam decision (recorded per the T5-verb brief): the lint module exposes NO
reusable non-primitive function — its rule orchestration lives entirely inside
the ``notebook-lint`` primitive body (only private ``_check_*`` helpers exist).
So this verb does NOT recompute lint findings; it accepts them OPAQUELY via the
spec (caller chains ``notebook-lint`` → ``notebook-audit-view``). Nothing is
extracted from the lint primitive.

This file mirrors ``ops/notebook/lint.py``'s home (inside ``ops/notebook/``,
beside the pure builder it wraps) rather than the ``ops/`` role root: it reads a
single subject (the ``state.audit_source`` section model + the view builder over
it), so the subject-imports lint is satisfied without a role-root exemption.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.notebook_audit_view import (
    NotebookAuditViewResult,
    NotebookAuditViewSpec,
    NotebookSectionView,
    NotebookViewAssertion,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.audit_view import AuditView, build_audit_view, render_markdown
from hpc_agent.state.audit_source import parse_percent_source

__all__ = ["notebook_audit_view"]

_PRIMITIVE = "notebook-audit-view"


def _read_source_file(experiment_dir: Path, relpath: str, *, kind: str) -> str:
    """Read a caller-declared ``.py`` (source or template) or raise SpecInvalid.

    A missing/unreadable file is a malformed spec (it points at a file that is
    not there), NOT a projection — there is nothing to view. Loud, matching T4's
    ``notebook-lint`` refusal wording.
    """
    path = Path(relpath)
    if not path.is_absolute():
        path = Path(experiment_dir) / path
    if not path.is_file():
        raise errors.SpecInvalid(f"notebook-audit-view {kind} file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(
            f"notebook-audit-view {kind} file could not be read: {path} ({exc})"
        ) from exc


def _to_result(view: AuditView) -> NotebookAuditViewResult:
    """Project the pure :class:`AuditView` into the wire result + its markdown."""
    sections = [
        NotebookSectionView(
            slug=sv.slug,
            classification=sv.classification,
            tier=sv.tier,
            section_sha=sv.section_sha,
            template_section_sha=sv.template_section_sha,
            diff=list(sv.diff),
            assertions=[
                NotebookViewAssertion(test=a.test, lineno=a.lineno, msg=a.msg)
                for a in sv.assertions
            ],
            lint_flags=[dict(f) for f in sv.lint_flags],
            view_sha=sv.view_sha,
        )
        for sv in view.sections
    ]
    return NotebookAuditViewResult(
        sections=sections,
        dropped_template_slugs=list(view.dropped_template_slugs),
        source_module_sha=view.source_module_sha,
        template_module_sha=view.template_module_sha,
        view_sha=view.view_sha,
        markdown=render_markdown(view),
    )


@primitive(
    name=_PRIMITIVE,
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Render the deterministic per-section audit VIEW of a source .py "
            "against its template. Read-only, no SSH. Each SOURCE section carries "
            "its classification (inherited / added / modified, by hash), "
            "diff-from-template, static assertion table, opaque chained lint "
            "flags, D-attention tier (auto_cleared iff inherited + no flags + "
            "assertions green), and a per-section view_sha (what a sign-off binds). "
            "The result includes `markdown` — the code-rendered projection the "
            "skill relays VERBATIM. Recomputed from the .py on every call."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookAuditViewSpec,
        schema_ref=SchemaRef(input="notebook_audit_view"),
    ),
    agent_facing=True,
)
def notebook_audit_view(
    *, experiment_dir: Path, spec: NotebookAuditViewSpec
) -> NotebookAuditViewResult:
    """Build the deterministic audit view for *source* against *template*.

    Parses both ``.py`` (percent format), builds the :class:`AuditView` over the
    source sections with the caller's OPAQUE ``lint_findings`` and optional
    ``receipt``, and returns the canonical projection plus its verbatim-relay
    ``markdown``. Idempotent by construction — derived state recomputed from the
    ``.py`` on every call.

    Raises :class:`errors.SpecInvalid` on an unreadable source/template path or a
    malformed percent-format module (bad/duplicate/misplaced section marker — the
    parser's own boundary guards).
    """
    experiment_dir = Path(experiment_dir)
    source = parse_percent_source(_read_source_file(experiment_dir, spec.source, kind="source"))
    template = parse_percent_source(
        _read_source_file(experiment_dir, spec.template, kind="template")
    )

    view = build_audit_view(
        source,
        template,
        spec.lint_findings,
        receipt=spec.receipt,
        attention_order=spec.attention_order,
    )
    return _to_result(view)
