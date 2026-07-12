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

Local, no SSH, no scheduler. Derived state, recomputed from the ``.py`` on disk on
every call, so it can never drift from a second source of truth. Two deterministic
writes, both idempotent and neither a second source of truth: (1) the
TRUSTED-DISPLAY render — per section, the content-addressed render file
(:mod:`hpc_agent.ops.notebook.render_store`) the T8 sign-off gate requires,
DETERMINISTIC bytes at a ``view_sha``-addressed path; and (2) — for a CANONICAL
view only — a per-HUMAN-REQUIRED-section render-relay-due MARKER (the omission
gate's second producer, run-#11 item 3) whose ``view_sha12`` key token the
relay-audit Stop hook discharges only when the render actually reaches the human
(deduplicated on the key token, so re-viewing arms nothing new). A PREVIEW view
journals no marker.

Two modes (the full-view-recompute upgrade): the DEFAULT is the CANONICAL view —
the verb recomputes the lint SERVER-SIDE from the audit's RECORDED roots
(interview.json), reads the journaled fresh receipts, and applies the recorded
attention order, all through the ONE definition
(:func:`~hpc_agent.ops.notebook.canonical.build_canonical_view`) the T8 sign-off
gate also recomputes against — so the skill's default flow produces gate-acceptable
view_shas. An OVERRIDE (explicit ``lint_findings``, an inline ``receipt``, or
explicit roots/order differing from the recorded config) makes the result a
PREVIEW (``canonical=false``) built from the caller's inputs verbatim; its
view_shas the gate may refuse. The ``canonical`` result field says which it is.

This file mirrors ``ops/notebook/lint.py``'s home (inside ``ops/notebook/``,
beside the pure builder it wraps) rather than the ``ops/`` role root: it reads a
single subject (the ``state.audit_source`` section model + the view builder over
it), so the subject-imports lint is satisfied without a role-root exemption.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.notebook_audit_view import (
    NotebookAuditViewResult,
    NotebookAuditViewSpec,
    NotebookSectionView,
    NotebookViewAssertion,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.audit_view import (
    HUMAN_REQUIRED,
    AuditView,
    build_audit_view,
    render_markdown,
    render_summary_markdown,
)
from hpc_agent.ops.notebook.canonical import (
    AuditConfig,
    build_canonical_view,
    read_recorded_config,
)
from hpc_agent.ops.notebook.render_store import _VIEW_SHA_ADDRESS_LEN, write_render
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.data_trace import read_trace
from hpc_agent.state.notebook_audit import (
    RENDER_RELAY_DUE_RECORD_KIND,
    record_scope_relay_due,
)

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


def _to_result(
    experiment_dir: Path, audit_id: str, view: AuditView, *, canonical: bool, full: bool = False
) -> NotebookAuditViewResult:
    """Project the pure :class:`AuditView` into the wire result + its markdown.

    WRITES the content-addressed TRUSTED-DISPLAY render file for EVERY section
    (always — cheap + deterministic; the T8 sign-off gate requires the render for
    what-the-human-saw to exist on disk, ``render_store``). Each section's wire
    projection carries the experiment-relative ``render_path`` that was written.
    ``canonical`` records whether this view's view_shas match what the T8 gate
    recomputes (a canonical build) or are a preview the gate may refuse.

    ``full`` selects the ``markdown`` payload (run-#12 finding 12, B1): the DEFAULT
    (``full=False``) is the DIGEST — per-section metadata + render-file pointers,
    NO diff/assertion/flag body bytes; ``full=True`` is the whole-body render for a
    harness that still model-relays. The per-section wire projection NEVER carries
    the unified diff (it lived twice — in the markdown AND a structured array); the
    diff stays derivable from the render file and the ``full`` markdown.

    The omission gate's SECOND producer (run-#11 item 3, "a link is not a
    relay"): for a CANONICAL view only, each HUMAN-REQUIRED section arms a
    render-relay-due marker (:func:`~hpc_agent.state.notebook_audit.record_scope_relay_due`)
    whose single key token is that section's ``view_sha12`` — the same hash the
    render filename is addressed by. The relay-audit Stop hook discharges it only
    when that sha12 appears in the turn, so a render that reached the human as an
    unread file link (run #11's live failure) blocks the stop exactly like an
    unrelayed ``notebook-status`` verdict. Deduplicated on (record_kind,
    key_tokens), so re-viewing the same section arms nothing new (the verb stays
    idempotent). PREVIEW views (``canonical=false``) and auto_cleared sections
    journal NOTHING — the narrow set is deliberate (D8 applied to the gate).
    """
    sections = []
    render_paths: dict[str, str] = {}
    for sv in view.sections:
        written = write_render(experiment_dir, audit_id=audit_id, view=sv)
        if canonical and sv.tier == HUMAN_REQUIRED:
            # The view_sha12 embedded in the render filename is exactly the
            # discharge token — relaying the render (its sha12) closes the marker.
            record_scope_relay_due(
                experiment_dir,
                scope_kind="notebook",
                scope_id=audit_id,
                record_kind=RENDER_RELAY_DUE_RECORD_KIND,
                key_tokens=[sv.view_sha[:_VIEW_SHA_ADDRESS_LEN]],
            )
        try:
            # as_posix() so the persisted/relayed relpath is forward-slash on
            # every platform — a Windows backslash in a core-computed relpath
            # would leak into records and break cross-platform reproduction.
            rel = written.relative_to(Path(experiment_dir).resolve()).as_posix()
        except ValueError:
            rel = written.as_posix()
        render_paths[sv.slug] = rel
        sections.append(
            NotebookSectionView(
                slug=sv.slug,
                classification=sv.classification,
                tier=sv.tier,
                section_sha=sv.section_sha,
                template_section_sha=sv.template_section_sha,
                assertions=[
                    NotebookViewAssertion(test=a.test, lineno=a.lineno, msg=a.msg)
                    for a in sv.assertions
                ],
                lint_flags=[dict(f) for f in sv.lint_flags],
                view_sha=sv.view_sha,
                render_path=rel,
            )
        )
    # B1: DEFAULT emits the digest (metadata + render-file pointers, no body
    # bytes); `full` emits the whole-body render for a still-model-relaying harness.
    markdown = render_markdown(view) if full else render_summary_markdown(view, render_paths)
    return NotebookAuditViewResult(
        sections=sections,
        dropped_template_slugs=list(view.dropped_template_slugs),
        source_module_sha=view.source_module_sha,
        template_module_sha=view.template_module_sha,
        canonical=canonical,
        view_sha=view.view_sha,
        markdown=markdown,
    )


@primitive(
    name=_PRIMITIVE,
    verb="query",
    # Honest registry metadata: the view MATERIALIZES the content-addressed
    # trusted-display render per section (deterministic, idempotent cache —
    # same inputs, byte-identical file). It stays a query verb: it appends no
    # journal record and mutates no state a reader consumes; the render is
    # what the T8 sign-off gate requires to exist.
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/renders/<audit_id>/<slug>.<view_sha12>.md"),
        SideEffect(
            "file_write",
            "<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl (render "
            "relay-due marker, CANONICAL human-required sections only)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Render the deterministic per-section audit VIEW of a source .py "
            "against its template. Read-only, no SSH. Each SOURCE section carries "
            "its classification (inherited / added / modified, by hash), "
            "diff-from-template, static assertion table, lint flags, D-attention "
            "tier (auto_cleared iff inherited + no flags + assertions green), and a "
            "per-section view_sha (what a sign-off binds). By DEFAULT the CANONICAL "
            "view: lint recomputed server-side from the audit's recorded roots + "
            "journaled receipts, so its view_shas match what the T8 gate accepts "
            "(`canonical: true`); explicit findings/receipt/roots make it a preview. "
            "The result includes `markdown` — the code-rendered projection the "
            "skill relays VERBATIM. By DEFAULT `markdown` is the DIGEST (per-section "
            "metadata + render-file pointers, NO diff/assertion/flag body bytes — "
            "run-#12 finding 12); pass `full: true` for the whole-body render. "
            "Recomputed from the .py on every call. Per section it also WRITES the "
            "content-addressed TRUSTED-DISPLAY render file (.hpc/renders/<audit_id>/"
            "<slug>.<view_sha12>.md) and returns its render_path — the artifact the "
            "T8 sign-off gate requires (and where the full per-section body lives)."
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
    """Build the audit view for *source* against *template*.

    By DEFAULT builds the CANONICAL view (server-recomputed lint from the recorded
    roots, journaled fresh receipts, recorded attention order — the one definition
    the T8 gate recomputes against); an explicit ``lint_findings`` / ``receipt`` /
    roots override yields a PREVIEW (``canonical=false``). Returns the projection
    plus its verbatim-relay ``markdown``. Idempotent by construction — derived
    state recomputed from the ``.py`` on every call.

    Raises :class:`errors.SpecInvalid` on an unreadable source/template path or a
    malformed percent-format module (bad/duplicate/misplaced section marker — the
    parser's own boundary guards).
    """
    experiment_dir = Path(experiment_dir)
    # Loud existence check with the verb's own wording (the canonical build re-reads).
    source = parse_percent_source(_read_source_file(experiment_dir, spec.source, kind="source"))
    parse_percent_source(_read_source_file(experiment_dir, spec.template, kind="template"))

    # The recorded audit configuration (interview.json) vs the caller's effective
    # one. Roots/order default to the RECORDED values; an explicit override, an
    # inline receipt, or explicit lint_findings makes the view a PREVIEW.
    recorded = read_recorded_config(experiment_dir, spec.audit_id)
    effective = AuditConfig(
        input_roots=spec.input_roots if spec.input_roots is not None else recorded.input_roots,
        source_roots=spec.source_roots if spec.source_roots is not None else recorded.source_roots,
        attention_order=(
            spec.attention_order if spec.attention_order is not None else recorded.attention_order
        ),
        output_roots=spec.output_roots if spec.output_roots is not None else recorded.output_roots,
    )
    preview_forced = spec.receipt is not None or bool(spec.lint_findings)
    if preview_forced:
        # PREVIEW: honor the caller's inline receipt / findings verbatim (the read-
        # only inspection path) — its view_shas the T8 gate may refuse.
        template = parse_percent_source(
            _read_source_file(experiment_dir, spec.template, kind="template")
        )
        view = build_audit_view(
            source,
            template,
            spec.lint_findings,
            receipt=spec.receipt,
            attention_order=effective.attention_order,
            # The section join reads the SAME on-disk audit trace in the preview
            # path so the runtime-evidence summary is not silently dropped when a
            # caller overrides lint/receipt (A16 B3-LEAN). Tolerant read → [].
            audit_traces=read_trace(experiment_dir, "audit", spec.audit_id, 0),
        )
        canonical = False
    else:
        # CANONICAL: the ONE definition — server-recomputed lint from the effective
        # roots, journaled fresh receipts, effective attention order.
        view = build_canonical_view(
            experiment_dir,
            audit_id=spec.audit_id,
            source_relpath=spec.source,
            template_relpath=spec.template,
            cfg=effective,
        )
        # It is CANONICAL (gate-acceptable) only when the effective config equals
        # the recorded one — an override that still recomputes lint is a preview.
        canonical = effective == recorded
    return _to_result(experiment_dir, spec.audit_id, view, canonical=canonical, full=spec.full)
