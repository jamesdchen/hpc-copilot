"""Pydantic models for the ``program-verify`` query verb.

``program-verify`` is the PROGRAM-level projection over recorded reproduction
judgments (program-level reproduction, phase 1 — receipts-first). A *program* is
the run-set behind a citable results table: its identity is EMERGENT (the
``extract-recipe`` seed's minimal contributing run-set), never a declared-up-front
key. Given either an explicit constituent list or an ``extract-recipe`` seed
(a campaign or a reduced-table path), it reads the reproduction evidence ALREADY
on record for each constituent — pair receipts
(``reproduction_receipts.jsonl``) reachable via the ``reproduces`` back-link, and
the determinism-fingerprint ledger samples for the run's identity — classifies
each constituent from the RECEIPT vocabulary (never a fresh comparison), folds a
program roll-up, and materializes a write-once signed program manifest.

The verdict DISCLOSES, never gates: like ``verify-reproduction`` a not-fully-
reproduced program is a ``needs_decision`` FINDING (exit-0), never an error. The
verb is a PURE projection over recorded judgments — it never re-compares a
metric, never names one, and mints no new evidence.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire.queries.verify_reproduction import ReproTolerance

#: The five RECEIPT-DERIVED per-constituent classifications. Each reads off the
#: reproduction receipt's own ``overall`` vocabulary (``auto_cleared`` / ``match``
#: / ``mismatch`` / ``incomparable`` / a human-cleared ``needs_verdict``), never a
#: fresh comparison:
#:
#: * ``reproduced_within_tolerance`` — a receipt whose verdict cleared: an
#:   ``auto_cleared`` (or an empty-ledger exact ``match``) receipt, or a
#:   ``needs_verdict`` receipt whose fingerprint sample the human ACCEPTED
#:   (the admission join — a recorded judgment, not this verb's).
#: * ``mismatch_on_record`` — a receipt whose ``overall`` is ``mismatch`` (a
#:   recorded contradiction: the discovered-nondeterminism finding, never hidden).
#: * ``evidence_incomparable`` — a receipt whose ``overall`` is ``incomparable``,
#:   or an unresolved ``needs_verdict`` (routed to the human, not yet cleared).
#: * ``evidence_stale_identity`` — the driving receipt's ``original`` identity legs
#:   (cmd_sha / tasks_py_sha / executor; data_sha when present) no longer match the
#:   CURRENT sidecar: the verdict was earned at a superseded identity, so it is not
#:   current evidence. Folds with ``no_reproduction_on_record`` (stale evidence is
#:   no current evidence) but is DISTINCTLY named so the drifted leg is disclosed.
#: * ``no_reproduction_on_record`` — no reproduction receipt for the constituent.
ConstituentClassification = Literal[
    "reproduced_within_tolerance",
    "mismatch_on_record",
    "evidence_incomparable",
    "evidence_stale_identity",
    "no_reproduction_on_record",
]


class ProgramVerifySpec(BaseModel):
    """Input spec for ``program-verify`` — EITHER an explicit list OR a seed.

    Exactly one program-identity source:

    * ``run_ids`` — an explicit constituent list (the caller already knows the
      run-set behind the table).
    * ``campaign_id`` / ``aggregate_path`` — an ``extract-recipe`` seed; the
      program identity is EMERGENT (the seed's minimal contributing run-set, with
      canary / superseded / dead-end members mechanically excluded). When the
      walk degrades to the G4a harvest-receipt proxy, that is disclosed in the
      result's ``gaps`` exactly as ``extract-recipe`` discloses it.

    ``tolerance`` is an OPTIONAL passthrough consistent with ``verify-reproduction``'s
    model — it is echoed for provenance only (this verb never re-compares, so the
    tolerance that judged each pair is the one recorded on that pair's receipt).
    """

    model_config = ConfigDict(extra="forbid", title="program-verify input spec")

    run_ids: list[str] | None = Field(
        default=None,
        description=(
            "Explicit constituent run-set — the runs behind the citable table. "
            "Mutually exclusive with campaign_id / aggregate_path."
        ),
    )
    campaign_id: str | None = Field(
        default=None,
        description=(
            "extract-recipe seed: the program identity is the campaign's minimal "
            "contributing run-set. Mutually exclusive with the other two."
        ),
    )
    aggregate_path: str | None = Field(
        default=None,
        description=(
            "extract-recipe seed: a reduced-metrics artifact whose contributing "
            "run-set is the program identity. Mutually exclusive with the other two."
        ),
    )
    tolerance: ReproTolerance | None = Field(
        default=None,
        description=(
            "Optional tolerance passthrough (verify-reproduction's model), echoed "
            "for provenance. program-verify never re-compares metrics — the tolerance "
            "that judged each pair is the one recorded on that pair's receipt."
        ),
    )

    @model_validator(mode="after")
    def _one_source(self) -> ProgramVerifySpec:
        """Require exactly one program-identity source."""
        sources = [
            bool(self.run_ids),
            bool((self.campaign_id or "").strip()),
            bool((self.aggregate_path or "").strip()),
        ]
        if sum(sources) != 1:
            raise ValueError(
                "program-verify requires exactly one program-identity source: a "
                "non-empty run_ids list XOR campaign_id XOR aggregate_path."
            )
        return self


class ConstituentVerdict(BaseModel):
    """One constituent run's recorded reproduction evidence — a projection.

    Every field is READ off records already on disk: the classification off the
    reproduction receipt's ``overall`` vocabulary, the reason code-rendered from
    the driving receipt's own keys, the identity fields off the sidecar, and the
    drift disclosures echoed verbatim from the receipt (never re-derived). The
    verb never names a metric value.
    """

    model_config = ConfigDict(extra="forbid", title="program constituent verdict")

    run_id: str = Field(description="The constituent run's identity.")
    classification: ConstituentClassification = Field(
        description="The receipt-derived classification of this constituent's reproduction evidence."
    )
    reason: str = Field(
        description="Code-rendered reason, read off the driving receipt's own keys (never LLM-authored)."
    )
    receipt_count: int = Field(
        ge=0, description="How many reproduction receipts name this constituent as the original."
    )
    repro_run_ids: list[str] = Field(
        default_factory=list,
        description="The reproduction run ids whose receipts named this constituent (the reproduces back-link).",
    )
    cmd_sha: str | None = Field(
        default=None, description="The constituent's param identity (sidecar cmd_sha)."
    )
    tasks_py_sha: str | None = Field(
        default=None, description="The constituent's code identity (sidecar tasks_py_sha)."
    )
    executor: str | None = Field(
        default=None, description="The constituent's executor identity (sidecar executor)."
    )
    fingerprint_samples: int = Field(
        ge=0,
        description="Count of determinism-fingerprint ledger samples for this constituent's current identity.",
    )
    driving_receipt: dict[str, Any] | None = Field(
        default=None,
        description="The reproduction receipt that determined the classification, embedded verbatim; null when none on record.",
    )
    env_identity: dict[str, Any] | None = Field(
        default=None,
        description="The driving receipt's environment-lock disclosure, echoed read-only; null when the receipt carries none.",
    )
    hw_identity: dict[str, Any] | None = Field(
        default=None,
        description="The driving receipt's hardware-placement disclosure, echoed read-only; null when the receipt carries none.",
    )
    data_identity: dict[str, Any] | None = Field(
        default=None,
        description="The driving receipt's data-identity disclosure, echoed read-only; null when the receipt carries none.",
    )
    diverged_stage: str | None = Field(
        default=None,
        description="The driving receipt's localized diverging stage (data-trace interlock), echoed read-only; null when none.",
    )


class ProgramVerifyResult(BaseModel):
    """The program roll-up — a projection over recorded reproduction judgments.

    A not-fully-reproduced program is a ``needs_decision`` FINDING (exit-0), never
    an error: the verdict DISCLOSES, never gates. ``recipe_signature`` is the
    ``extract-recipe`` seed's signature (null for an explicit list);
    ``program_signature`` is this program manifest's own deterministic digest —
    the write-once file is ``program-<program_signature[:12]>.json``.
    """

    model_config = ConfigDict(extra="forbid", title="program-verify output data")

    program_schema_version: int = Field(
        ge=1, description="Bumped when the emitted program-verify shape changes incompatibly."
    )
    seed_kind: Literal["explicit", "campaign", "aggregate"] = Field(
        description="Which program-identity source resolved the run-set."
    )
    seed_ref: str = Field(
        description="The seed's identity / path verbatim (the run-set list for explicit)."
    )
    recipe_signature: str | None = Field(
        default=None,
        description="The extract-recipe seed's signature over the minimal run-set; null for an explicit list.",
    )
    program_signature: str = Field(
        description="This program manifest's deterministic 64-hex digest — the write-once file name key."
    )
    resolved_run_ids: list[str] = Field(
        default_factory=list, description="The resolved constituent run-set."
    )
    constituents: list[ConstituentVerdict] = Field(
        default_factory=list, description="Per-constituent recorded reproduction evidence."
    )
    reproduced_count: int = Field(
        ge=0, description="How many constituents classified reproduced_within_tolerance (k of N)."
    )
    total: int = Field(ge=0, description="The constituent count (N).")
    overall: ConstituentClassification = Field(
        description=(
            "The program roll-up: the most severe constituent classification "
            "(mismatch_on_record > no_reproduction_on_record = evidence_stale_identity "
            "> evidence_incomparable > reproduced_within_tolerance)."
        )
    )
    needs_decision: bool = Field(
        description="True unless every constituent reproduced_within_tolerance — a FINDING the human decides on (exit-0); a stale-identity constituent forces it True."
    )
    reason: str = Field(
        description="Code-rendered one-line program summary (k/N reproduced + per-class counts + gaps)."
    )
    gaps: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The extract-recipe identity-walk gaps passed through (G4a table→run-set link, pack-csv opaque, operator-bypass); empty for an explicit list.",
    )
    manifest_path: str | None = Field(
        default=None,
        description="Absolute path of the write-once program manifest (.hpc/provenance/program-<sig[:12]>.json).",
    )
    manifest_delta: str | None = Field(
        default=None,
        description="Disclosed content-drift delta when a prior manifest for the same seed had a different signature; null on a clean/idempotent write.",
    )
    markdown: str = Field(
        default="",
        description="The code-rendered program report (deterministic; LLM-free render path).",
    )
