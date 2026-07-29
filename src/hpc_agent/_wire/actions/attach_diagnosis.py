"""Pydantic models for the ``attach-diagnosis`` verb (park-time diagnosis seam).

``attach-diagnosis`` is the durable ATTACH channel for a read-only park-time
investigator agent: it stores an agent-authored diagnosis dossier as an OPAQUE,
provenance-marked proposal beside the run's terminal records
(``.hpc/runs/<run_id>.diagnosis.json``). The verb validates SHAPE only — the
content is agent judgment by design and is never interpreted, gated on, or
copied into a trusted surface (decision briefs, answer-menu options, gate
inputs). Provenance (``authored_by: "agent"``, ``attached_at``) is stamped by
the state-layer writer, never accepted from the caller.

Ungated on purpose: advisory data spends nothing — no greenlight, no consent,
no budget. Re-attach OVERWRITES (newest diagnosis wins; it is advisory).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class DiagnosisEvidenceExcerpt(BaseModel):
    """One quoted log/artifact excerpt: WHERE it came from + the lines quoted."""

    model_config = ConfigDict(extra="forbid", title="attach-diagnosis evidence excerpt")

    path: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "The local file the excerpt was read from — one of the paths the "
            "diagnosis-request named (run sidecar, worker log, journal record). "
            "A pointer for the human to open; never fetched by any surface."
        ),
    )
    lines: str = Field(
        max_length=4000,
        description=(
            "The quoted lines (verbatim excerpt, bounded). Display-only "
            "advisory matter — no gate or trusted surface ever reads this."
        ),
    )


class DiagnosisProposedAction(BaseModel):
    """One DRAFTED recovery option — a proposal the human may read, never an answer."""

    model_config = ConfigDict(extra="forbid", title="attach-diagnosis proposed action")

    label: str = Field(
        min_length=1,
        max_length=200,
        description="Short name of the drafted option (e.g. 'raise mem_mb and resubmit').",
    )
    rationale: str = Field(
        max_length=2000,
        description="Why the investigator drafted it (agent judgment, advisory).",
    )
    suggested_response_text: str = Field(
        max_length=2000,
        description=(
            "Text the HUMAN could choose to type as their park answer. Never "
            "auto-filled, never rendered as an answer-menu option (the menu is "
            "code-authored data only) — the human reads it from the dossier "
            "render and decides."
        ),
    )


class AttachDiagnosisSpec(BaseModel):
    """Input spec for ``attach-diagnosis`` — shape validation only."""

    model_config = ConfigDict(extra="forbid", title="attach-diagnosis input spec")

    run_id: RunIdStrict
    classification: str = Field(
        min_length=1,
        max_length=100,
        description=(
            "The investigator's classification, named from the CLOSED set the "
            "diagnosis-request disclosed (infra.failure_signatures."
            "CLASSIFIER_CATEGORIES) or the literal 'unmatched' when no catalog "
            "category fits. Membership is enforced by the verb; free-text "
            "categories are refused."
        ),
    )
    evidence_excerpts: list[DiagnosisEvidenceExcerpt] = Field(
        default_factory=list,
        max_length=20,
        description="Quoted log evidence (path + lines), bounded.",
    )
    proposed_actions: list[DiagnosisProposedAction] = Field(
        default_factory=list,
        max_length=10,
        description="Drafted recovery options, bounded. Proposals only.",
    )


class AttachDiagnosisResult(BaseModel):
    """Shape of the ``data`` field on an ``attach-diagnosis`` envelope."""

    model_config = ConfigDict(extra="forbid", title="attach-diagnosis output data")

    run_id: RunIdStrict
    path: str = Field(description="Absolute path of the written <run_id>.diagnosis.json dossier.")
    attached_at: str = Field(
        description="ISO-8601 UTC provenance stamp written by the state layer."
    )
    classification: str = Field(description="The classification as stored.")
    proposed_actions_count: int = Field(
        description="How many drafted actions the dossier carries (the pointer count)."
    )
    overwrote: bool = Field(
        description=(
            "True when a prior diagnosis existed and was replaced (re-attach "
            "overwrites — newest advisory dossier wins)."
        )
    )
