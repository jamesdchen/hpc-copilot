"""``notebook-status`` — read the per-section audit state of an audited source.

A read-only ``query`` primitive (notebook-audit T6). Given an experiment dir, a
source ``.py`` relpath, a template ``.py`` relpath, and an ``audit_id``, it:

1. parses the source + template as jupytext percent-format modules
   (:func:`hpc_agent.state.audit_source.parse_percent_source`) — the template's
   section slugs are the REQUIRED inventory;
2. replays the ``audit_id`` decision journal and reduces every required section
   to a T6 status via :mod:`hpc_agent.state.notebook_audit` (whose drift verdict
   routes through the ONE attestation kernel);
3. returns per-section ``{slug, status, current/signed section_sha, view_sha,
   attestor}`` plus the whole-module ``passed`` gate predicate.

Pure local read — no SSH, no scheduler, no write. Derived state, recomputed from
the ``.py`` on disk + the journal on every call, so it can never drift from a
second source of truth.

This file lives at the ``ops/`` *role root* (sibling to ``export_dossier.py`` /
``trace.py``, NOT inside ``ops/notebook/``) because it reads across subjects —
the ``state.audit_source`` section model and the ``state.notebook_audit``
reduction over the decision journal. The subject-imports lint short-circuits for
role-root files, so the cross-subject reads here are allowed by construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.notebook_status import (
    NotebookSectionStatus,
    NotebookStatusResult,
    NotebookStatusSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.notebook_audit import audit_module

__all__ = ["notebook_status"]


def _read_percent_module(experiment_dir: Path, relpath: str, label: str) -> str:
    """Read an experiment-relative ``.py`` source, or raise a naming SpecInvalid.

    A missing file is a caller-input error (the relpath is wrong), so it surfaces
    as :class:`errors.SpecInvalid` naming which of source/template was absent —
    not a bare ``FileNotFoundError`` the envelope would classify as internal.
    """
    path = (Path(experiment_dir) / relpath).resolve()
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"notebook-status: {label} not found at {relpath!r} (resolved {path})"
        ) from exc
    except OSError as exc:
        raise errors.SpecInvalid(
            f"notebook-status: {label} at {relpath!r} could not be read: {exc}"
        ) from exc


@primitive(
    name="notebook-status",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Report the per-section audit state of an audited source .py against "
            "its template inventory and audit_id journal. Read-only, no SSH. Each "
            "required (template) section reduces to signed_current / auto_cleared "
            "/ signed_stale / unsigned (drift-revoked by construction); `passed` "
            "is the graduation gate's whole-module predicate (every section "
            "current). Section shas are recomputed from the .py on every call."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookStatusSpec,
        schema_ref=SchemaRef(input="notebook_status"),
    ),
    agent_facing=True,
)
def notebook_status(*, experiment_dir: Path, spec: NotebookStatusSpec) -> NotebookStatusResult:
    """Reduce every required section of an audited source to its T6 audit status.

    Parses the source + template ``.py`` (percent format), takes the template's
    section slugs as the required inventory, and reduces each against its CURRENT
    source sha and the ``audit_id`` journal. A required template section absent
    from the source reads ``unsigned`` (nothing to sign). ``passed`` is true iff
    every required section is current (``signed_current`` or ``auto_cleared``).

    Idempotent by construction: derived state recomputed from the ``.py`` + the
    journal on every call.

    Raises :class:`errors.SpecInvalid` on an unreadable source/template path or a
    malformed percent-format module (bad/duplicate/misplaced section marker — the
    parser's own boundary guards).
    """
    experiment_dir = Path(experiment_dir)
    source = parse_percent_source(_read_percent_module(experiment_dir, spec.source, "source"))
    template = parse_percent_source(_read_percent_module(experiment_dir, spec.template, "template"))

    module_audit = audit_module(
        experiment_dir,
        spec.audit_id,
        source=source,
        required_slugs=template.slugs,
    )
    sections = [
        NotebookSectionStatus(
            slug=a.slug,
            status=a.status,
            current_section_sha=a.current_section_sha,
            signed_section_sha=a.signed_section_sha,
            view_sha=a.view_sha,
            attestor=a.attestor,
        )
        for a in module_audit.sections
    ]
    return NotebookStatusResult(
        audit_id=spec.audit_id,
        sections=sections,
        passed=module_audit.passed,
    )
