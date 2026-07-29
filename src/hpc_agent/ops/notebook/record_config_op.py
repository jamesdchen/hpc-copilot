"""``notebook-record-config`` — the STANDALONE audit's configuration seat.

Run-#10 live finding (``docs/design/notebook-audit.md`` Amendment 2): the
canonical-config read (``ops/notebook/canonical.py::read_recorded_config``)
read ONLY interview.json's ``audited_source`` block, so a standalone audit —
one that never opted in through the interview — ran ROOTLESS-canonical: the
lint recomputed with EMPTY roots, the template-mandated ``source_roots``
engine-drift binding was silently inactive, and executes-live flags fired
against no roots. No seat existed to record the configuration.

This ``mutate`` verb IS that seat. It journals the audit configuration
(``input_roots`` / ``source_roots`` / ``attention_order`` / ``output_roots`` —
all OPAQUE relpath strings) as a ``notebook-audit-config`` record in the SAME
notebook-audit journal the audit's decisions live in
(:func:`hpc_agent.state.notebook_audit.record_audit_config`). The canonical
read then falls back to it — interview ``audited_source`` WINS when present
(the opt-in path owns the config; ONE source of truth), else the journaled
record, else empty as before.

Two refusals (both :class:`hpc_agent.errors.SpecInvalid`):

* interview.json already carries an ``audited_source`` block for this
  ``audit_id`` — the opt-in path owns the config; recording a second copy
  would make two seats disagree about the same audit.
* a config record already exists for this ``audit_id`` — the config is
  IMMUTABLE-PER-AUDIT: every view_sha and sign-off is downstream of it, so a
  mutable config would silently re-key an audit trail. Superseding a recorded
  config means a NEW ``audit_id``.

One loud disclosure (never a refusal): recording a config into an audit that
already has journal entries (views signed, receipts recorded, ...) SUCCEEDS
but carries a ``warning`` — the config enters every canonical view recompute,
so EVERY view_sha moves and prior sign-offs will read stale against the new
canonical view. That is correct, disclosed behavior: drift-revocation is the
kernel's job, and a stale sign-off simply reads unsigned.

This file lives inside the ``notebook`` subject (beside the canonical reader
whose fallback it feeds), reaching only same-subject ``ops.notebook.*`` and
the ``state.*`` substrate — the subject-imports lint is satisfied by
construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_record_config import (
    NotebookRecordConfigResult,
    NotebookRecordConfigSpec,
)
from hpc_agent.ops.notebook.canonical import read_interview_audited_source
from hpc_agent.state import notebook_audit
from hpc_agent.state.decision_journal import read_decisions

__all__ = ["notebook_record_config"]

_PRIMITIVE = "notebook-record-config"

#: The loud late-record disclosure (see the module docstring). A constant so the
#: wording is pinned by tests, never improvised per call.
_LATE_RECORD_WARNING = (
    "this audit_id already had journal entries when the config was recorded: "
    "the recorded config enters every canonical view recompute, so EVERY "
    "view_sha moves and prior sign-offs will read STALE against the new "
    "canonical view. That is correct, disclosed behavior (drift = unsigned) — "
    "re-run notebook-audit-view and re-sign the sections that still matter."
)


# Dispatched by the merged ``notebook-record`` verb
# (``ops/notebook/record_op.py``) on ``kind: "config"``; no longer a
# standalone registration. The seat's semantics (immutable-per-audit, the
# interview-owns-the-config refusal, the loud late-record warning) are
# unchanged and stay pinned by this module's tests.
def notebook_record_config(
    *, experiment_dir: Path, spec: NotebookRecordConfigSpec
) -> NotebookRecordConfigResult:
    """Journal the standalone audit's configuration, immutably-per-audit.

    Refuses (:class:`errors.SpecInvalid`) when interview.json already carries
    an ``audited_source`` block for ``spec.audit_id`` (the opt-in path owns the
    config — one source of truth) or when a ``notebook-audit-config`` record
    already exists for it (immutable-per-audit; superseding = a new
    ``audit_id``). When the audit already has OTHER journal entries the record
    still lands, but the result carries the loud ``warning`` — every view_sha
    moves, prior sign-offs read stale (disclosed, never silent).
    """
    experiment_dir = Path(experiment_dir)

    # ONE source of truth: the interview opt-in path owns the config when it
    # recorded one — two seats must never disagree about the same audit.
    if read_interview_audited_source(experiment_dir, spec.audit_id) is not None:
        raise errors.SpecInvalid(
            f"{_PRIMITIVE}: interview.json already carries an audited_source "
            f"block for audit_id={spec.audit_id!r} — the interview opt-in path "
            "owns this audit's configuration (one source of truth). Standalone "
            "config recording is for audits with NO audited_source block."
        )

    records = read_decisions(experiment_dir, "notebook", spec.audit_id)

    # IMMUTABLE-PER-AUDIT: every view_sha and sign-off is downstream of the
    # config; a second record would silently re-key the audit trail.
    if notebook_audit.read_audit_config(experiment_dir, spec.audit_id) is not None:
        raise errors.SpecInvalid(
            f"{_PRIMITIVE}: a config record already exists for "
            f"audit_id={spec.audit_id!r} — the audit configuration is "
            "immutable per audit (every view_sha is downstream of it). To "
            "supersede it, start a NEW audit_id and record the config there."
        )

    # The loud late-record disclosure: any pre-existing journal entry (a
    # sign-off, an auto-clear, a receipt) was produced against the OLD
    # (rootless) canonical view; the recorded config moves every view_sha.
    warning = _LATE_RECORD_WARNING if records else None

    notebook_audit.record_audit_config(
        experiment_dir,
        audit_id=spec.audit_id,
        input_roots=spec.input_roots,
        source_roots=spec.source_roots,
        attention_order=spec.attention_order,
        output_roots=spec.output_roots,
        goal=spec.goal,
        task_axes=spec.task_axes,
        observables=spec.observables,
    )

    return NotebookRecordConfigResult(
        audit_id=spec.audit_id,
        input_roots=list(spec.input_roots),
        source_roots=list(spec.source_roots),
        attention_order=list(spec.attention_order) if spec.attention_order is not None else None,
        output_roots=list(spec.output_roots),
        goal=spec.goal,
        task_axes=list(spec.task_axes) if spec.task_axes is not None else None,
        observables=list(spec.observables) if spec.observables is not None else None,
        warning=warning,
    )
