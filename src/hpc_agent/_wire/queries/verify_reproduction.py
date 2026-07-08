"""Pydantic models for the ``verify-reproduction`` query's spec + result.

``verify-reproduction`` compares the reduced metrics of a reproduction run
against those of the original it names (via the sidecar ``reproduces`` link),
under a caller-owned tolerance, and writes a durable receipt. The comparator
carries NO metric vocabulary — it compares opaque numbers, naming and judging
left to the human above (``docs/design/reproduction-receipt.md``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict
from hpc_agent._wire.queries.determinism import (
    DeterminismSampleRecord,
    EnvelopeApplied,
    TierReason,
)


class KeyTolerance(BaseModel):
    """Per-metric-key tolerance override.

    Both bounds optional; an absent bound is simply not applied. When BOTH
    are absent the key is compared EXACTLY (``==``) — same as supplying no
    tolerance at all.
    """

    model_config = ConfigDict(extra="forbid", title="per-key reproduction tolerance")

    abs_tol: float | None = Field(
        default=None,
        ge=0.0,
        description="Absolute tolerance: |orig - repro| <= abs_tol counts as a match.",
    )
    rel_tol: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Relative tolerance: |orig - repro| / max(|orig|, |repro|) <= rel_tol "
            "counts as a match."
        ),
    )


class ReproTolerance(BaseModel):
    """Caller-owned tolerance for the reproduction comparison.

    All fields optional. When every field is absent (and ``per_key`` empty)
    the comparison is EXACT — numeric metrics must be bit-equal. A default
    bound applies to every numeric key that has no ``per_key`` override; a
    ``per_key`` entry fully replaces the default for that key.
    """

    model_config = ConfigDict(extra="forbid", title="reproduction tolerance spec")

    default_abs_tol: float | None = Field(
        default=None,
        ge=0.0,
        description="Absolute tolerance applied to every numeric key lacking a per_key override.",
    )
    default_rel_tol: float | None = Field(
        default=None,
        ge=0.0,
        description="Relative tolerance applied to every numeric key lacking a per_key override.",
    )
    per_key: dict[str, KeyTolerance] = Field(
        default_factory=dict,
        description="Per-metric-key tolerance overrides, keyed by the (flattened) metric key.",
    )


class ReproKeyVerdict(BaseModel):
    """One per-key entry of a reproduction receipt (schema_version 2).

    The v1 fields (``key`` … ``tolerance_applied``) are byte-preserved; the two
    determinism-fingerprint additions are ADDITIVE and default ``None`` so a v1
    receipt line (which carries neither) still parses under this model — the
    ledger is append-only and old lines must remain readable.
    """

    model_config = ConfigDict(extra="forbid", title="reproduction per-key verdict")

    key: str = Field(
        description="The flattened metric key (opaque; the comparator never reads its meaning)."
    )
    original: Any = Field(default=None, description="Value on the original side (opaque).")
    repro: Any = Field(default=None, description="Value on the reproduction side (opaque).")
    abs_diff: float | None = Field(
        default=None, description="|original - repro| for numeric keys; null otherwise."
    )
    rel_diff: float | None = Field(
        default=None, description="Relative diff for numeric keys; null otherwise."
    )
    verdict: Literal["match", "mismatch", "incomparable"] = Field(
        description="Per-key comparison verdict."
    )
    tolerance_applied: dict[str, float | None] | None = Field(
        default=None,
        description="Caller-owned tolerance applied to this key, verbatim; null when exact/measured.",
    )
    # ── schema_version 2 additions (D-verdict-wire) ──────────────────────────
    envelope_applied: EnvelopeApplied | None = Field(
        default=None,
        description=(
            "The measured envelope + evidence weight that judged this key (D-envelope "
            "resolution disclosure); null when no envelope applied (exact-class key, "
            "or a caller override decided the key)."
        ),
    )
    tier_reason: TierReason | None = Field(
        default=None,
        description="Why this key reached its tolerance-class verdict (D-verdict-wire); null when no envelope/tolerance participated.",
    )


class ReproductionReceipt(BaseModel):
    """The durable receipt record appended to the reproduction receipts ledger.

    schema_version 2 EXTENDS the v1 receipt with the tiered verdict and the
    partiality accounting (design center 5). Every v2 addition is optional with a
    None/False default, so a v1 line — ``overall`` in {match, mismatch,
    incomparable}, no partiality fields, per_key entries without envelope/tier —
    parses UNCHANGED under this model. The receipt still rides
    ``VerifyReproductionResult.receipt`` as an opaque dict on the wire; this model
    is the authoring shape consumers construct + validate against.
    """

    model_config = ConfigDict(extra="forbid", title="reproduction receipt (schema_version 2)")

    ts: str = Field(description="ISO-8601 UTC append timestamp.")
    schema_version: int = Field(
        description="Receipt schema version (1 = pre-fingerprint, 2 = tiered)."
    )
    original: dict[str, Any] = Field(
        description="The original run's identity, lifted verbatim off its sidecar."
    )
    repro: dict[str, Any] = Field(
        description="The reproduction run's identity, lifted verbatim off its sidecar."
    )
    tolerance_spec: dict[str, Any] | None = Field(
        default=None, description="Verbatim echo of the caller-owned tolerance (null when exact)."
    )
    per_key: list[ReproKeyVerdict] = Field(
        default_factory=list, description="Per-key comparison verdicts."
    )
    overall: Literal["match", "mismatch", "incomparable", "auto_cleared", "needs_verdict"] = Field(
        description=(
            "Overall verdict. v1: match / mismatch / incomparable. v2 also emits "
            "auto_cleared (code attestation) / needs_verdict (routed to the human)."
        ),
    )
    sources: dict[str, Any] = Field(
        description="Which artifact each side was loaded from (provenance)."
    )
    # ── schema_version 2 additions (design center 5 — no-silent-caps partiality) ──
    partial: bool = Field(
        default=False,
        description="True when only a subset of tasks was compared (partial reproduction).",
    )
    task_indices: list[int] | None = Field(
        default=None,
        description="The exact task indices compared on a partial receipt; null for a full receipt.",
    )
    uncompared_keys: int | None = Field(
        default=None,
        description="Count of metric keys NOT compared (partial disclosure); null when full/unknown.",
    )
    uncompared_tasks: int | None = Field(
        default=None,
        description="Count of tasks NOT compared (partial disclosure); null when full/unknown.",
    )


class VerifyReproductionSpec(BaseModel):
    """Input spec for ``verify-reproduction``.

    ``tolerance`` absent (``None``) — or present with every bound absent —
    means an EXACT comparison.
    """

    model_config = ConfigDict(extra="forbid", title="verify-reproduction input spec")

    original_run_id: RunIdStrict = Field(
        description="Run id of the ORIGINAL run being reproduced.",
    )
    repro_run_id: RunIdStrict = Field(
        description=(
            "Run id of the reproduction run. Its sidecar's `reproduces` field "
            "MUST name original_run_id, or the verb refuses (SpecInvalid)."
        ),
    )
    tolerance: ReproTolerance | None = Field(
        default=None,
        description="Caller-owned tolerance; None (or all-absent) → exact comparison.",
    )


class VerifyReproductionResult(BaseModel):
    """Result of a reproduction comparison.

    A mismatch or incomparable is a SUCCESSFUL run (exit-0, needs_decision=True)
    — a discovered nondeterminism is the feature working, never an error.
    """

    model_config = ConfigDict(extra="forbid", title="verify-reproduction output")

    stage_reached: Literal["match", "mismatch", "incomparable", "auto_cleared", "needs_verdict"] = (
        Field(
            description=(
                "Overall verdict. v1: match / mismatch (any key mismatched) / incomparable. "
                "v2 also emits auto_cleared (every deviation inside a well-evidenced envelope — "
                "a code attestation, zero human attention) and needs_verdict (a thin-envelope / "
                "novelty / incomparable residue routed to the human with the evidence brief)."
            ),
        )
    )
    needs_decision: bool = Field(
        description=(
            "True for a FINDING the human decides on (mismatch / incomparable / needs_verdict); "
            "False when the comparison auto-cleared (exact match or well-evidenced envelope)."
        ),
    )
    reason: str = Field(
        description="Code-rendered one-line summary: matched/mismatched/incomparable key counts + verdict.",
    )
    receipt: dict[str, Any] = Field(
        description="The full receipt record appended to reproduction_receipts.jsonl (self-contained).",
    )
    receipt_path: str = Field(
        description="Absolute path of the append-only receipts ledger this verification appended to.",
    )
    appended_sample: DeterminismSampleRecord | None = Field(
        default=None,
        description=(
            "The determinism-fingerprint sample this comparison appended to the "
            "experiment's ledger (D-consume: verify appends the comparison as a new "
            "sample). Null when no sample was minted (e.g. a v1-only comparison, or "
            "missing artifacts). Echoed so a consumer sees the evidence just recorded."
        ),
    )
