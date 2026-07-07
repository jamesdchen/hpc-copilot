"""``notebook-auto-clear`` — journal CODE auto-clear attestations, un-fakeably.

The CODE-attestor mirror of the human ``notebook-sign-off`` (notebook-audit,
``docs/design/notebook-audit.md`` D-attention + D5). A ``mutate`` verb: for every
source section the D-attention tiering deems ``auto_cleared`` and that is not
already cleared at its current hash, it appends a ``notebook-auto-clear``
attestation via :func:`hpc_agent.state.notebook_audit.record_auto_clear` (block
``notebook-auto-clear``, ``response="auto_cleared"``, ``attestor:"code"``). This
is the ONLY agent-facing writer of those records — without it, template-inherited
untouched sections could never pass the T9 graduation gate (which requires every
required section auto_cleared-or-signed at its current hash).

**Un-fakeability (the load-bearing constraint).** The verb RECOMPUTES everything
server-side through the ONE canonical-view definition
(:func:`~hpc_agent.ops.notebook.canonical.build_canonical_view`, shared with the
T8 sign-off gate and the render plugin): real lint from the recorded roots,
journaled fresh receipts, recorded attention order. It accepts NO caller-supplied
lint findings, NO tier claims, and NO caller-supplied lint ROOTS (adversarial
review F2 — supplying ``input_roots`` / ``source_roots`` is a loud ``SpecInvalid``
refusal; the roots come from the audit's RECORDED config unconditionally, so a
caller cannot point the lint at a directory of planted dummy files to stop a
section flagging). A caller passing empty findings therefore cannot
launder a flagged or modified section into ``auto_cleared`` — the tier is
recomputed from the freshly-parsed source + the freshly-recomputed lint. The
auto-clear record is
then bound through the ONE attestation kernel against the freshly-parsed section
sha (:func:`record_auto_clear` → ``attestation.bind``), so a machine clearance can
no more assert a sha into existence than a human sign-off can (D5 lock 2).

**Receipts are JOURNALED, not trusted from the caller (T10).** The
assertions-green leg of the tier needs execution evidence; v1 accepted a
caller-supplied ``receipt`` and trusted it verbatim (the laundering hole:
``{slug: {error: False}}`` greened an asserted section with no execution and no
freshness key). The spec's ``receipt`` field is GONE; the verb instead reads the
journaled render receipts (:func:`~hpc_agent.state.notebook_audit.read_render_receipts`)
and feeds only the entries still FRESH at the current section sha to
:func:`build_audit_view` — a receipt is a code attestation bound to a section sha
(``notebook-record-receipt``), so it drifts stale by construction the moment the
section moves and greens nothing thereafter.

**Idempotency (append-only, honest accounting).** Before appending, each
auto_cleared candidate is reduced against the existing journal
(:func:`hpc_agent.state.notebook_audit.audit_section`): a section already
CURRENT — an auto-clear OR a human sign-off at this hash — is skipped
``already-current`` (a re-run appends nothing). A section whose prior auto-clear
went stale (its source moved) reduces to ``unsigned`` and is re-cleared at the
NEW hash with a NEW record — never a mutation of the old one (the journal is
append-only; the newest-valid record wins by the kernel's own rule).

This file lives inside the ``notebook`` subject (beside the pure builder and the
lint it recomputes), reaching only same-subject ``ops.notebook.*`` and the
``state.*`` substrate — the subject-imports lint is satisfied by construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.notebook_auto_clear import (
    NotebookAutoClearedSection,
    NotebookAutoClearResult,
    NotebookAutoClearSkipped,
    NotebookAutoClearSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.audit_view import AUTO_CLEARED as VIEW_AUTO_CLEARED
from hpc_agent.ops.notebook.canonical import (
    AuditConfig,
    build_canonical_view,
    read_recorded_config,
)
from hpc_agent.state import notebook_audit
from hpc_agent.state.decision_journal import read_decisions

__all__ = ["notebook_auto_clear"]

_PRIMITIVE = "notebook-auto-clear"

#: The honest reason an already-cleared-current section is skipped (an auto-clear
#: OR a human sign-off at this hash — nothing to append).
_ALREADY_CURRENT = "already-current"


def _read_source_file(experiment_dir: Path, relpath: str, *, kind: str) -> str:
    """Read a caller-declared ``.py`` (source or template), or raise SpecInvalid.

    A missing/unreadable file points at a file that is not there — a malformed
    spec, NOT a section that fails to clear. Loud, matching the ``notebook-lint``
    / ``notebook-audit-view`` refusal wording.
    """
    path = Path(relpath)
    if not path.is_absolute():
        path = Path(experiment_dir) / path
    if not path.is_file():
        raise errors.SpecInvalid(f"notebook-auto-clear {kind} file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(
            f"notebook-auto-clear {kind} file could not be read: {path} ({exc})"
        ) from exc


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl"),
    ],
    error_codes=[errors.SpecInvalid],
    # Append-only but idempotent by construction: a re-run at unchanged hashes
    # appends nothing (every auto_cleared section reduces to already-current).
    # The natural equivalence key is the audit scope.
    idempotent=True,
    idempotency_key="audit_id",
    cli=CliShape(
        help=(
            "Journal CODE auto-clear attestations for the template-inherited, "
            "clean sections of an audited source .py — the machine mirror of a "
            "notebook sign-off. RECOMPUTES the lint + D-attention tier server-side "
            "(caller findings / tier claims are never trusted), then appends a "
            "notebook-auto-clear record (attestor=code, bound to the recomputed "
            "section hash) for every auto_cleared section not already cleared at "
            "its current hash. human_required sections are never cleared; a re-run "
            "at unchanged hashes appends nothing. Pure local reads + journal "
            "appends, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookAutoClearSpec,
        schema_ref=SchemaRef(input="notebook_auto_clear"),
    ),
    agent_facing=True,
)
def notebook_auto_clear(
    *, experiment_dir: Path, spec: NotebookAutoClearSpec
) -> NotebookAutoClearResult:
    """Append a CODE auto-clear for every clean, inherited, untouched section.

    Parses the source + template, RECOMPUTES the lint findings and the
    D-attention view server-side (never trusting a caller), and for each
    ``auto_cleared`` section not already current in the ``audit_id`` journal,
    appends a ``notebook-auto-clear`` attestation bound to the freshly-recomputed
    section sha. Returns the honest ``cleared`` / ``skipped`` accounting.

    Idempotent (``audit_id``): a re-run at unchanged hashes clears nothing; an
    edited section re-clears at its new hash with a NEW append-only record.

    Raises :class:`errors.SpecInvalid` on an unreadable source/template path or a
    malformed percent-format module (bad/duplicate/misplaced section marker), or
    (via :func:`record_auto_clear`) on a sha that fails the recompute bind.
    """
    experiment_dir = Path(experiment_dir)
    # Loud existence check with the notebook-auto-clear wording (build_canonical_view
    # would also refuse, but with generic wording); the canonical builder re-reads.
    _read_source_file(experiment_dir, spec.source, kind="source")
    _read_source_file(experiment_dir, spec.template, kind="template")

    # LAUNDERING GUARD (adversarial review F2). The auto-clear tier is recomputed
    # from the audit's RECORDED roots ONLY — a caller may NOT supply its own lint
    # roots. Accepting ``spec.input_roots`` / ``spec.source_roots`` was a journal-
    # effective laundering vector: a hostile caller could point ``input_roots`` at
    # a directory of PLANTED dummy files so an executes-live section stops flagging
    # its missing literal, launder it into ``auto_cleared``, and journal that code
    # clearance for the graduation gate to honor. The roots come from
    # interview.json unconditionally; supplying either field is a loud refusal, so
    # the boundary is documented at the seam rather than silently overridden.
    if spec.input_roots or spec.source_roots:
        raise errors.SpecInvalid(
            "notebook-auto-clear does not accept caller-supplied input_roots / "
            "source_roots (journal-effective laundering class): the server-side "
            "lint recompute uses the audit's RECORDED roots (interview.json's "
            "audited_source block) so a section cannot be laundered into "
            "auto_cleared with planted roots. Remove them from the spec."
        )

    # RECOMPUTE the CANONICAL view SERVER-SIDE through the ONE definition
    # (``build_canonical_view``): real lint from the recorded roots, journaled
    # fresh receipts, recorded attention order — the SAME view the T8 sign-off gate
    # recomputes, so an auto-clear's view_sha agrees with a later sign-off's by
    # construction (the un-fakeability constraint: caller findings / tier claims are
    # never trusted). The roots are the audit's RECORDED roots
    # (interview.json audited_source), read UNCONDITIONALLY — never a caller override.
    recorded = read_recorded_config(experiment_dir, spec.audit_id)
    cfg = AuditConfig(
        input_roots=recorded.input_roots,
        source_roots=recorded.source_roots,
        attention_order=recorded.attention_order,
    )
    view = build_canonical_view(
        experiment_dir,
        audit_id=spec.audit_id,
        source_relpath=spec.source,
        template_relpath=spec.template,
        cfg=cfg,
    )

    # Read the journal once; each section's idempotency decision is independent
    # (distinct slugs), so appends within this call don't affect one another.
    records = read_decisions(experiment_dir, "notebook", spec.audit_id)

    cleared: list[NotebookAutoClearedSection] = []
    skipped: list[NotebookAutoClearSkipped] = []
    for sv in view.sections:
        if sv.tier != VIEW_AUTO_CLEARED:
            # human_required — a modified / lint-flagged / ungreen-assertion
            # section the code may never clear (only a human sign-off can).
            skipped.append(NotebookAutoClearSkipped(section=sv.slug, reason=sv.tier))
            continue
        audit = notebook_audit.audit_section(records, sv.slug, sv.section_sha)
        if audit.status in notebook_audit.PASSING_STATUSES:
            # Already current at this hash (auto_cleared OR a human sign-off) —
            # nothing to append. Never downgrade a human sign-off to a code one.
            skipped.append(NotebookAutoClearSkipped(section=sv.slug, reason=_ALREADY_CURRENT))
            continue
        # Bind + append. recompute wired to the freshly-parsed sha (the parse IS
        # the recompute — the value is server-computed, never caller-supplied).
        notebook_audit.record_auto_clear(
            experiment_dir,
            audit_id=spec.audit_id,
            section=sv.slug,
            section_sha=sv.section_sha,
            recompute=sv.section_sha,
            view_sha=sv.view_sha,
        )
        cleared.append(
            NotebookAutoClearedSection(
                section=sv.slug,
                section_sha=sv.section_sha,
                view_sha=sv.view_sha,
            )
        )

    return NotebookAutoClearResult(audit_id=spec.audit_id, cleared=cleared, skipped=skipped)
