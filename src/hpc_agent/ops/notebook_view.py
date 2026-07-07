"""Top-level facade re-exporting the notebook audit-view builder + tier vocabulary.

Mirrors ``ops/field_ownership.py``: a subject file (``ops/decision/journal.py``'s
notebook sign-off authorship gate, T8) reaches the ``ops/notebook`` subject's
deterministic view builder through this TOP-LEVEL facade — a direct
``from hpc_agent.ops.notebook.audit_view import ...`` from inside the ``decision``
subject trips the subject-import lint (``scripts/lint_subject_imports.py``). This
file lives directly under ``ops/`` (a role root, NOT a subject directory), so the
lint never scans it and it may import any subject freely. One source of truth:
this binds, never copies, the view builder and its constants — the tier is
computed in exactly one place (``ops/notebook/audit_view.py``).
"""

from __future__ import annotations

from hpc_agent.ops.notebook.audit_view import (
    AUTO_CLEARED,
    HUMAN_REQUIRED,
    SUBJECT_KIND,
    AuditView,
    SectionView,
    build_audit_view,
)
from hpc_agent.ops.notebook.render_store import read_render_header, render_path

__all__ = [
    "AUTO_CLEARED",
    "HUMAN_REQUIRED",
    "SUBJECT_KIND",
    "AuditView",
    "SectionView",
    "build_audit_view",
    "render_path",
    "read_render_header",
]
