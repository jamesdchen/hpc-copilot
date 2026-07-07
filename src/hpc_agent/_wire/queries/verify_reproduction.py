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

    stage_reached: Literal["match", "mismatch", "incomparable"] = Field(
        description="Overall verdict: match, mismatch (any key mismatched), or incomparable.",
    )
    needs_decision: bool = Field(
        description="True for mismatch/incomparable (a FINDING the human decides on); False for match.",
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
