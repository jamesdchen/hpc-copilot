"""``notebook-render`` — project an audited source .py as a jupytext notebook.

Role (a) of the export: the PORTABILITY ARTIFACT. Given the sealed records (the
audited source ``.py`` + template + the notebook decision journal), render a
``.ipynb`` whose per-section AUDIT CELLS carry the status/tier/hashes the CORE
view computed, and, for human-required sections, a SIGN-OFF scaffold cell the
human types into (feeding role (b), ``notebook-ingest-signoffs``). Optionally
executes the notebook in the current env and journals sha-bound render receipts
through the CORE record-receipt op.

The notebook is ALWAYS a render, never the source of truth — the header cell says
so, and no path here ever writes the ``.py``. jupytext/nbformat/nbclient are the
renderer's deps and never enter core.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import jupytext

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliShape
from hpc_agent.ops.notebook.audit_view import HUMAN_REQUIRED
from hpc_agent.ops.notebook.canonical import AuditConfig, build_canonical_view, read_recorded_config
from hpc_agent.ops.notebook.render_store import write_render
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.notebook_audit import audit_section

from . import _annotate
from ._models import (
    NotebookRenderResult,
    NotebookRenderSpec,
    RenderedSection,
)

if TYPE_CHECKING:
    from hpc_agent.ops.notebook.audit_view import SectionView

__all__ = ["notebook_render"]

_PRIMITIVE = "notebook-render"


def _read_rel(experiment_dir: Path, relpath: str, *, what: str) -> str:
    """Read an experiment-relative ``.py``, or raise SpecInvalid loudly."""
    path = Path(relpath)
    if not path.is_absolute():
        path = experiment_dir / path
    if not path.is_file():
        raise errors.SpecInvalid(f"notebook-render {what} file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(f"notebook-render {what} file unreadable: {path} ({exc})") from exc


def _assemble(nb: Any, views: dict[str, SectionView], statuses: dict[str, str]) -> Any:
    """Insert the header + per-section audit / sign-off cells into *nb* in place.

    Audit cells precede each section-opening cell; a sign-off scaffold follows the
    audit cell for a HUMAN_REQUIRED section. Preamble cells (before the first
    marker) pass through after the header. All inserted cells are markdown — they
    carry no outputs, so they never affect a section's ``output_sha``.
    """
    out: list[Any] = [_annotate.header_cell()]
    for cell in nb.cells:
        opens = _annotate.cell_section_slug(cell)
        if opens is not None and opens in views:
            view = views[opens]
            out.append(
                _annotate.audit_cell(
                    opens,
                    status=statuses.get(opens, "unsigned"),
                    tier=view.tier,
                    classification=view.classification,
                    section_sha=view.section_sha,
                    view_sha=view.view_sha,
                )
            )
            if view.tier == HUMAN_REQUIRED:
                out.append(_annotate.signoff_cell(opens))
        out.append(cell)
    nb.cells = out
    return nb


def _record_receipts(
    experiment_dir: Path, spec: NotebookRenderSpec, per_section: dict[str, tuple[str, bool]]
) -> tuple[list[str], list[str]]:
    """Journal one sha-bound render receipt per section via the CORE op, in-process.

    Calling the core op function directly buys the bind/gate parity for free — each
    receipt is bound to the freshly-parsed section sha server-side, so the plugin
    cannot launder a receipt for a drifted section.
    """
    from hpc_agent._wire.actions.notebook_record_receipt import (
        NotebookReceiptEntry,
        NotebookRecordReceiptSpec,
    )
    from hpc_agent.ops.notebook.record_receipt_op import notebook_record_receipt

    entries = {
        slug: NotebookReceiptEntry(output_sha=output_sha, error=error)
        for slug, (output_sha, error) in per_section.items()
    }
    result = notebook_record_receipt(
        experiment_dir=experiment_dir,
        spec=NotebookRecordReceiptSpec(audit_id=spec.audit_id, source=spec.source, entries=entries),
    )
    return (
        [r.section for r in result.recorded],
        [s.section for s in result.skipped],
    )


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/_notebooks/<audit_id>.ipynb"),
        SideEffect("file_write", "<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl"),
    ],
    error_codes=[errors.SpecInvalid],
    # Writes a notebook file (and, with record_receipts, appends receipts). Not
    # byte-idempotent: an executed re-render appends fresh receipts (append-only,
    # like the core record-receipt op).
    idempotent=False,
    cli=CliShape(
        help=(
            "Render an audited source .py as a jupytext notebook: per-section audit "
            "cells (status/tier/hashes from the core view) plus sign-off scaffolds "
            "for human-required sections. --execute runs the notebook in the current "
            "env and computes a per-section output_sha; --record_receipts (needs "
            "--execute) journals sha-bound render receipts via the core op. The "
            "notebook is always a RENDER, never the source of truth. A non-executed "
            "render is byte-deterministic. Ships no JSON schema: the Pydantic spec "
            "model validates at the seam."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookRenderSpec,
    ),
    agent_facing=True,
)
def notebook_render(*, experiment_dir: Path, spec: NotebookRenderSpec) -> NotebookRenderResult:
    """Render *spec.source* as an annotated (optionally executed) notebook.

    Raises :class:`errors.SpecInvalid` on an unreadable/ malformed source or
    template, or when ``record_receipts`` is set without ``execute``.
    """
    experiment_dir = Path(experiment_dir)
    if spec.record_receipts and not spec.execute:
        raise errors.SpecInvalid(
            "notebook-render: record_receipts=true requires execute=true (a receipt "
            "attests an EXECUTION; there is nothing to record without running)."
        )

    source_text = _read_rel(experiment_dir, spec.source, what="source")
    template_text = _read_rel(experiment_dir, spec.template, what="template")
    source = parse_percent_source(source_text)
    parse_percent_source(template_text)  # loud on a malformed template
    # Build the CANONICAL view (server-recomputed lint from the recorded roots +
    # journaled fresh receipts) so the render files this writes are addressed by
    # the SAME view_shas the core T8 sign-off gate recomputes — a later sign-off
    # (typed + ingested, or committed directly) finds its trusted-display artifact.
    # The recorded attention order is honored; an explicit spec override wins.
    recorded_cfg = read_recorded_config(experiment_dir, spec.audit_id)
    cfg = AuditConfig(
        input_roots=recorded_cfg.input_roots,
        source_roots=recorded_cfg.source_roots,
        attention_order=(
            spec.attention_order
            if spec.attention_order is not None
            else recorded_cfg.attention_order
        ),
    )
    view = build_canonical_view(
        experiment_dir,
        audit_id=spec.audit_id,
        source_relpath=spec.source,
        template_relpath=spec.template,
        cfg=cfg,
    )
    views = {sv.slug: sv for sv in view.sections}

    # Write the content-addressed TRUSTED-DISPLAY render for every section via the
    # CORE render store, against the CURRENT source. A subsequent sign-off (typed in
    # the rendered notebook and ingested, or committed directly) needs that artifact
    # to satisfy the core T8 sign-off gate's trusted-display lock.
    for sv in view.sections:
        write_render(experiment_dir, audit_id=spec.audit_id, view=sv)

    records = read_decisions(experiment_dir, "notebook", spec.audit_id)
    statuses = {
        sect.slug: audit_section(records, sect.slug, sect.section_sha).status
        for sect in source.sections
    }

    nb = jupytext.reads(source_text, fmt="py:percent")

    per_section: dict[str, tuple[str, bool]] = {}
    if spec.execute:
        from nbclient import NotebookClient

        # allow_errors: a raising cell must be RECORDED (its error output feeds the
        # section's error flag), never abort the render.
        NotebookClient(nb, kernel_name="python3", allow_errors=True).execute()
        slugs = _annotate.assign_cell_sections(nb.cells)
        by_slug: dict[str, list[Any]] = {}
        for cell, slug in zip(nb.cells, slugs, strict=True):
            if slug is not None:
                by_slug.setdefault(slug, []).append(cell)
        per_section = {slug: _annotate.section_output_sha(cells) for slug, cells in by_slug.items()}

    _assemble(nb, views, statuses)
    _annotate.normalize_notebook(nb)

    # The canonicalizer identity that binds every output_sha. Core's receipt-entry
    # model forbids extra keys, so it rides the render RESULT + the notebook's own
    # metadata (recorded AFTER normalize, which resets nb.metadata) — never the
    # receipt entry. Only an executed render has an output_sha to bind.
    canonicalizer: str | None = None
    canonicalizer_version: str | None = None
    if spec.execute:
        identity = _annotate.stamp_canonicalizer(nb)
        canonicalizer = identity["canonicalizer"]
        canonicalizer_version = identity["canonicalizer_version"]

    output_rel = spec.output_path or f"_notebooks/{spec.audit_id}.ipynb"
    output_path = Path(output_rel)
    if not output_path.is_absolute():
        output_path = experiment_dir / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_annotate.write_notebook(nb), encoding="utf-8")

    recorded: list[str] = []
    skipped: list[str] = []
    if spec.record_receipts:
        recorded, skipped = _record_receipts(experiment_dir, spec, per_section)

    return NotebookRenderResult(
        audit_id=spec.audit_id,
        output_path=str(output_path),
        sections=[
            RenderedSection(slug=sv.slug, status=statuses.get(sv.slug, "unsigned"), tier=sv.tier)
            for sv in view.sections
        ],
        executed=spec.execute,
        receipts_recorded=recorded,
        receipts_skipped=skipped,
        canonicalizer=canonicalizer,
        canonicalizer_version=canonicalizer_version,
    )
