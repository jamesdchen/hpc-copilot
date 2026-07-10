"""Pydantic models for the ``notebook-record-config`` mutate verb (run-#10 seat).

Wire surface over :mod:`hpc_agent.ops.notebook.record_config_op` — the
STANDALONE audit's configuration seat. Run #10's live finding
(``docs/design/notebook-audit.md`` Amendment 2): a standalone audit (no
interview.json ``audited_source`` opt-in) had NO seat recording its
configuration, so the canonical view recomputed the lint with EMPTY roots —
the template-mandated ``source_roots`` engine-drift binding was silently
inactive, and executes-live flags fired against no roots.

This verb journals the audit configuration (``input_roots`` / ``source_roots``
/ ``attention_order`` / ``output_roots`` — all OPAQUE relpath strings, core
attaches no meaning) as a ``notebook-audit-config`` record in the SAME
notebook-audit journal the audit's decisions live in. The canonical-config
read (:func:`hpc_agent.ops.notebook.canonical.read_recorded_config`) then
falls back to it: an interview ``audited_source`` block WINS when present
(the opt-in path owns the config — one source of truth); else the journaled
record; else empty as before.

Immutable-per-audit: a second record for the same ``audit_id`` is refused —
superseding a recorded config means a NEW ``audit_id`` (every view_sha and
sign-off is downstream of the config, so a mutable config would silently
re-key an audit trail). Recording a config into an audit that already has
journal entries is allowed but LOUDLY disclosed via the result ``warning``:
every view_sha moves, so prior sign-offs will read stale — correct, named
behavior, never a silent re-key.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NotebookRecordConfigSpec(BaseModel):
    """Inputs to ``notebook-record-config`` — the standalone audit's config seat.

    All roots are OPAQUE relpath strings (core never attaches a meaning to a
    root). ``audit_id`` names the notebook decision journal the record is
    appended to.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-record-config input spec")

    audit_id: str = Field(
        min_length=1,
        description=(
            "The notebook decision-journal scope id (filesystem-safe slug) the "
            "config record is appended to (journal at "
            ".hpc/notebooks/<audit_id>.decisions.jsonl). Refused when "
            "interview.json already carries an audited_source block for this "
            "audit_id (the opt-in path owns the config) or when a config record "
            "already exists (immutable-per-audit; supersede with a NEW audit_id)."
        ),
    )
    input_roots: list[str] = Field(
        description=(
            "OPAQUE data-path roots the executes-live lint tests path literals "
            "against. Required (may be empty — an honest 'no data roots')."
        ),
    )
    source_roots: list[str] = Field(
        description=(
            "OPAQUE import roots the linked-sources lint resolves imports under "
            "— the template-mandated engine-drift binding run #10 found silently "
            "inactive for standalone audits. Required (may be empty)."
        ),
    )
    attention_order: list[str] | None = Field(
        default=None,
        description=(
            "Optional section-slug presentation ordering (null = source order). "
            "Participates in the module view_sha only."
        ),
    )
    output_roots: list[str] = Field(
        default_factory=list,
        description=(
            "OPAQUE WRITE-target roots: a path literal under one is a declared "
            "output, exempt from the executes-live not-exists flag (reported in "
            "declared_outputs, never flagged)."
        ),
    )
    observables: list[str] | None = Field(
        default=None,
        description=(
            "The OBSERVATION PLAN (A14): opaque declared-observable names the "
            "sanctioned runner (the notebook-render between-cell loop, T-R) looks "
            "up in the exec namespace and measures into runner-tier trace records. "
            "null = no plan (the loop is OFF; execution byte-identical)."
        ),
    )

    @field_validator("observables")
    @classmethod
    def _observables_nonempty(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not name.strip() for name in value):
            raise ValueError("observables entries must be non-empty strings")
        return value


class NotebookRecordConfigResult(BaseModel):
    """Echo of the journaled configuration, plus the loud late-record disclosure.

    ``warning`` is non-null when the audit already had journal entries at
    record time: recording a config moves every view_sha, so prior sign-offs
    will read stale — correct, disclosed behavior.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-record-config output data")

    audit_id: str
    input_roots: list[str] = Field(default_factory=list)
    source_roots: list[str] = Field(default_factory=list)
    attention_order: list[str] | None = None
    output_roots: list[str] = Field(default_factory=list)
    observables: list[str] | None = None
    warning: str | None = Field(
        default=None,
        description=(
            "Non-null when the audit_id already had journal entries (views "
            "signed, receipts recorded, ...) when the config was recorded: the "
            "recorded config enters every canonical view recompute, so EVERY "
            "view_sha moves and prior sign-offs will read stale against the new "
            "canonical view. Disclosed, never silent."
        ),
    )
