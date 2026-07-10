"""Pydantic models for the ``audit-handoff`` query verb (the audit→interview bridge).

Wire surface over :mod:`hpc_agent.ops.notebook.audit_handoff_op`. ``audit-handoff``
is a PURE READ that projects the durable audit records — the recorded audit-open
INTENT (``goal`` / ``task_axes`` on the ``notebook-audit-config`` seat), the
recorded config roots, and an AST scan of the audited source — into a DRAFT
``InterviewSpec`` the caller confirms and passes to the ``interview`` primitive.

Boundary posture (``docs/design/notebook-audit.md`` audit-handoff note): the verb
NEVER guesses. Every field is either DERIVED from a durable record / a syntactic
scan (and disclosed), or emitted as an explicit PLACEHOLDER the caller must fill
— a guessed field would become a journaled fact through the interview (the
``halo_expr`` failure class). ``summary_artifact`` writes and ``@register_run``
entry points are DETECTED-AND-DISCLOSED (multiple candidates are listed, never
silently chosen); the projection is deterministic (same records → byte-identical
draft).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditHandoffSpec(BaseModel):
    """Inputs to ``audit-handoff``.

    All relpaths / the ``audit_id`` are resolved against ``--experiment-dir``.
    The verb reads the ``audit_id``'s ``notebook-audit-config`` record (the
    audit-open seat: intent + roots) and AST-scans the ``source`` ``.py``.
    """

    model_config = ConfigDict(extra="forbid", title="audit-handoff input spec")

    audit_id: str = Field(
        min_length=1,
        description=(
            "The notebook decision-journal scope id (filesystem-safe slug) whose "
            "audit-open config/intent record is projected. Caller-authored."
        ),
    )
    source: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the audited source .py (jupytext percent "
            "format) — AST-scanned for @register_run entry points and "
            "$HPC_RESULT_DIR write candidates."
        ),
    )
    template: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the template .py the source was drafted "
            "from — carried verbatim into the draft audited_source block."
        ),
    )


class HandoffPlaceholder(BaseModel):
    """One field the caller MUST fill — the projection refuses to guess it."""

    model_config = ConfigDict(extra="forbid", title="audit-handoff placeholder")

    field: str = Field(description="The InterviewSpec field the caller must supply.")
    reason: str = Field(
        description="Why it is not derivable from the durable records (never guessed)."
    )


class AuditHandoffResult(BaseModel):
    """A DRAFT ``InterviewSpec`` projected from durable audit records.

    Derivable fields are filled and DISCLOSED; non-derivable fields are named in
    ``placeholders`` (never guessed). ``summary_artifact_candidates`` and
    ``entry_point_candidates`` are DETECTED-AND-DISCLOSED — the caller confirms
    which (if any) is right. The whole result is a deterministic function of the
    records + the source bytes (no timestamps, sorted candidate lists).
    """

    model_config = ConfigDict(extra="forbid", title="audit-handoff output data")

    audit_id: str
    goal: str | None = Field(
        default=None,
        description=(
            "The audit-open goal utterance, verbatim — null when the audit-open "
            "seat recorded none (then `goal` is also in `placeholders`)."
        ),
    )
    entry_point: dict[str, Any] | None = Field(
        default=None,
        description=(
            "A register_run entry_point block ({kind, run_name}) iff EXACTLY one "
            "@register_run function was found in the source. Null when zero or "
            ">1 were found (ambiguous → a placeholder; candidates listed in "
            "entry_point_candidates)."
        ),
    )
    entry_point_candidates: list[str] = Field(
        default_factory=list,
        description="Every @register_run function name found in the source, sorted.",
    )
    audited_source: dict[str, Any] = Field(
        description=(
            "The draft audited_source block: {source, audit_id, template} plus the "
            "recorded config roots (input_roots / source_roots / output_roots / "
            "attention_order). Always derivable from the verb inputs + the config "
            "seat."
        ),
    )
    task_axes: list[str] = Field(
        default_factory=list,
        description=(
            "The audit-open task-axis utterances, verbatim — the human's names for "
            "what varies across tasks. GUIDANCE for the caller's task_generator "
            "(which is always a placeholder — axis names are not a materializer)."
        ),
    )
    summary_artifact_candidates: list[str] = Field(
        default_factory=list,
        description=(
            "Result-relative paths the source WRITES under $HPC_RESULT_DIR, "
            "detected by the AST scan (sorted, deduped). Detected-and-disclosed — "
            "the caller confirms which is the citable summary artifact; the verb "
            "never picks."
        ),
    )
    unverifiable_result_writes: list[str] = Field(
        default_factory=list,
        description=(
            "Computed $HPC_RESULT_DIR path expressions the scanner could not "
            "reduce to a literal (an f-string with a computed tail, a non-literal "
            "join arg) — the honest gap, disclosed rather than silently dropped."
        ),
    )
    placeholders: list[HandoffPlaceholder] = Field(
        default_factory=list,
        description=(
            "Fields the caller MUST fill before the interview — never guessed. "
            "Always includes task_generator + task_count + produced_by; includes "
            "goal when no goal was recorded, and entry_point when zero or >1 "
            "@register_run functions were found."
        ),
    )
    disclosures: list[str] = Field(
        default_factory=list,
        description=(
            "Honest notes about what was and was not derivable (e.g. multiple "
            "summary_artifact candidates, no goal recorded at audit open, an "
            "ambiguous entry point). Advisory; never blocks."
        ),
    )
