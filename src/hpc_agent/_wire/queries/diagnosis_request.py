"""Pydantic models for the ``diagnosis-request`` verb (park-time diagnosis seam).

``diagnosis-request`` is the KERNEL side of the investigator seam: a pure,
code-composed read that tells a read-only investigator agent WHAT TO LOOK AT
for one parked run — the parked verb/stage/reason off the pending-decision
marker, the failure-signature matches the run's stores already hold (classified
by THE one catalog entry point, ``infra.failure_signatures.classify``), the
LOCAL log/artifact paths worth reading (paths only, never content), and the
closed category vocabulary the investigator must name its classification from.

The kernel never spawns the investigator and never consumes its judgment; this
verb only composes the request. Refuses (``precondition_failed``) for a run
that is not parked on a decision — there is nothing to investigate.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class DiagnosisRequestSpec(BaseModel):
    """Input spec for the ``diagnosis-request`` verb."""

    model_config = ConfigDict(extra="forbid", title="diagnosis-request input spec")

    run_id: RunIdStrict


class DiagnosisRequestResult(BaseModel):
    """Shape of the ``data`` field on a ``diagnosis-request`` envelope."""

    model_config = ConfigDict(extra="forbid", title="diagnosis-request output data")

    run_id: RunIdStrict
    block: str = Field(
        description="The parked block VERB off the pending-decision marker (e.g. 'submit-s2')."
    )
    workflow: str | None = Field(
        default=None, description="The workflow the parked block belongs to, or null."
    )
    awaiting_since: str | None = Field(
        default=None,
        description="When the run began awaiting the decision (ISO-8601 UTC), or null.",
    )
    stage_reached: str | None = Field(
        default=None,
        description=(
            "The parked block's stage, read from its durable terminal record "
            "(state/block_terminal) when one exists — e.g. 'canary_failed', "
            "'watching_anomaly'. Null when no terminal record is on disk."
        ),
    )
    reason: str | None = Field(
        default=None,
        description="The block's own code-composed park reason, from the terminal record.",
    )
    is_anomaly: bool = Field(
        description=(
            "True when (block, stage) is an anomaly terminator "
            "(infra.block_chain.ANOMALY_TERMINATORS), falling back to the park "
            "brief's own answer-menu OVERRIDE flag when the stage is unknown."
        )
    )
    signature_matches: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Failure-signature classifications over evidence the run's stores "
            "ALREADY hold (park brief / terminal record), each "
            "{source, error_class, suggested_fix, matched_pattern} from THE one "
            "catalog classifier (infra.failure_signatures.classify) — never a "
            "second matcher. Stored classified_error triples are relayed "
            "verbatim under their own source tag."
        ),
    )
    categories: list[str] = Field(
        description=(
            "The CLOSED classification vocabulary "
            "(infra.failure_signatures.CLASSIFIER_CATEGORIES, sorted). The "
            "investigator names its classification from this set, or "
            "'unmatched' when nothing fits — attach-diagnosis refuses anything "
            "else."
        )
    )
    read_paths: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Named LOCAL files the investigator should read (run sidecar, "
            "journal record, monitor log, block terminal, decision briefs) — "
            "paths only, existing files only, never content."
        ),
    )
    worker_logs: list[str] = Field(
        default_factory=list,
        description="Local detached-worker log paths for this run (paths only).",
    )
    attach_target: str = Field(
        description=(
            "Where attach-diagnosis will write the dossier "
            "(<run_id>.diagnosis.json beside the terminal records)."
        )
    )
    diagnosis_attached: bool = Field(
        default=False,
        description="True when a prior diagnosis dossier is already attached (re-attach overwrites).",
    )
    note: str = Field(
        description=(
            "The seam disclosure: the request is code-composed; the "
            "investigator's output is stored as an opaque agent-authored "
            "proposal, display-only, never a gate input."
        )
    )
