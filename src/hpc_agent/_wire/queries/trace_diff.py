"""Pydantic models for the ``trace-diff`` query's spec + result.

``trace-diff`` overlays TWO traces from the local store (``state/data_trace.py``,
T1) and reports, per stage and per atom, where their measurements DIVERGE — the
projection 5 of ``docs/design/data-trace.md`` (canary-vs-local, arm-vs-arm,
today-vs-last-known-good). It dispatches every comparison through T1's ONE
semantics registry (``comparison_for``) so the six semantics kinds (exact /
set-delta / tolerance / exact-per-key / equality-chain / exact-endpoints) have a
single definition, and it highlights the FIRST-DIVERGENCE — the earliest
``(stage, atom)`` where any comparison parts.

Differences are FACTS, never verdicts (the token pin, design §"Projections"):
the render says ``row_count rows 100 → 90``, never "wrong". The comparator
carries NO discipline vocabulary — it compares opaque atom values.

The tolerance is CALLER-OWNED (the ReproTolerance posture — core never invents
an epsilon): a ``tolerance`` absent, or present with every bound absent, means
an EXACT comparison of the tolerance-class atoms (``value_sketch`` per column,
``duration_ms`` / ``peak_mb``). This mirrors ``verify-reproduction``'s spec
shape (``_wire/queries/verify_reproduction.py``): a default abs/rel band plus
per-key overrides, keyed here by ``"<atom>"`` (scalar cost) or ``"<atom>:<col>"``
(a per-column sketch).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TraceKey(BaseModel):
    """A point-lookup key into the local trace store (one task's records).

    ``task`` defaults to ``0`` — a single-task local run stores under ``task-0``
    (the T1 recorded-answer), and most reference/canary comparisons are of one
    task, so the caller rarely supplies it.
    """

    model_config = ConfigDict(extra="forbid", title="trace-diff store key")

    scope_kind: Literal["run", "audit", "local"] = Field(
        description="The trace scope kind: `run` | `audit` | `local` (T1 TRACE_SCOPE_KINDS)."
    )
    scope_id: str = Field(
        min_length=1,
        description="The scope id (run_id / audit_id / cmd_sha12 for a local run).",
    )
    task: int = Field(
        default=0,
        ge=0,
        description="The task index within the scope (single-task local runs use 0).",
    )


class KeyTolerance(BaseModel):
    """Per-key tolerance override (mirrors verify-reproduction's ``KeyTolerance``).

    Both bounds optional; an absent bound is simply not applied. When BOTH are
    absent the key is compared EXACTLY — same as supplying no tolerance at all.
    """

    model_config = ConfigDict(extra="forbid", title="per-key trace tolerance")

    abs_tol: float | None = Field(
        default=None,
        ge=0.0,
        description="Absolute tolerance: |a - b| <= abs_tol is not a divergence.",
    )
    rel_tol: float | None = Field(
        default=None,
        ge=0.0,
        description="Relative tolerance: |a - b| / max(|a|, |b|) <= rel_tol is not a divergence.",
    )


class TraceTolerance(BaseModel):
    """Caller-owned tolerance for the tolerance-class atom comparisons.

    All fields optional. When every field is absent (and ``per_key`` empty) the
    comparison is EXACT — the no-invented-tolerance rule (core never picks an
    epsilon; the caller supplies the ReproTolerance posture or gets exact). A
    default bound applies to every numeric field lacking a ``per_key`` override;
    a ``per_key`` entry fully replaces the default for that key.

    Key convention: ``"duration_ms"`` / ``"peak_mb"`` for the scalar-cost atoms,
    ``"value_sketch:<column>"`` for a per-column sketch. Only the tolerance-class
    atoms consult it — exact / set-delta / exact-per-key / equality-chain /
    exact-endpoints atoms are ALWAYS compared exactly.
    """

    model_config = ConfigDict(extra="forbid", title="trace-diff tolerance spec")

    default_abs_tol: float | None = Field(
        default=None,
        ge=0.0,
        description="Absolute tolerance applied to every numeric field lacking a per_key override.",
    )
    default_rel_tol: float | None = Field(
        default=None,
        ge=0.0,
        description="Relative tolerance applied to every numeric field lacking a per_key override.",
    )
    per_key: dict[str, KeyTolerance] = Field(
        default_factory=dict,
        description="Per-key overrides, keyed by `<atom>` (scalar) or `value_sketch:<column>`.",
    )


class TraceDiffSpec(BaseModel):
    """Input spec for ``trace-diff``: two store keys + an optional tolerance.

    ``tolerance`` absent (``None``) — or present with every bound absent — means
    an EXACT comparison of every atom (the no-invented-tolerance rule).
    """

    model_config = ConfigDict(extra="forbid", title="trace-diff input spec")

    a: TraceKey = Field(description="The A-side trace key (e.g. today / this arm / the canary).")
    b: TraceKey = Field(
        description="The B-side trace key (e.g. last-known-good / the other arm / the local run)."
    )
    tolerance: TraceTolerance | None = Field(
        default=None,
        description="Caller-owned tolerance; None (or all-absent) → exact comparison.",
    )


class TraceEndpoint(BaseModel):
    """One side's key echo + what the store held for it (absence disclosed)."""

    model_config = ConfigDict(extra="forbid", title="trace-diff endpoint")

    scope_kind: str = Field(description="The side's trace scope kind.")
    scope_id: str = Field(description="The side's scope id.")
    task: int = Field(ge=0, description="The side's task index.")
    present: bool = Field(
        description="False when the store held no trace for this key (disclosed)."
    )
    stage_count: int = Field(ge=0, description="Number of stage records read for this side.")


class FirstDivergence(BaseModel):
    """The earliest diverging ``(stage, atom)`` — leads the render and the result.

    ``atom`` is ``null`` for a STRUCTURAL divergence (a stage present on one side
    only); ``kind`` names the semantics that parted (``exact`` / ``set-delta`` /
    …) or ``"structural"``.
    """

    model_config = ConfigDict(extra="forbid", title="trace-diff first divergence")

    stage: str = Field(description="The diverging stage name.")
    seq: int | None = Field(
        default=None, description="The diverging stage's seq (null only for a degenerate stage)."
    )
    atom: str | None = Field(
        default=None, description="The diverging atom name; null for a structural divergence."
    )
    kind: str = Field(description="The comparison semantics that parted, or 'structural'.")
    detail: str = Field(description="The factual one-line difference (never a verdict).")


class TraceDiffResult(BaseModel):
    """Result of overlaying two traces — facts, no verdicts."""

    model_config = ConfigDict(extra="forbid", title="trace-diff output")

    trace_schema_version: int = Field(
        ge=1, description="The T1 record schema version the diff was computed against."
    )
    a: TraceEndpoint = Field(description="The A-side endpoint echo + presence.")
    b: TraceEndpoint = Field(description="The B-side endpoint echo + presence.")
    clean: bool = Field(
        description="True when the two traces have NO divergence at any stage/atom."
    )
    aligned: bool = Field(
        description="True when every stage matched on (stage, seq) — no structural divergence."
    )
    tolerance_applied: dict[str, Any] | None = Field(
        default=None,
        description="Verbatim echo of the caller-owned tolerance (null when exact).",
    )
    first_divergence: FirstDivergence | None = Field(
        default=None,
        description="The earliest diverging (stage, atom); null on a clean diff.",
    )
    stages: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "One entry per aligned/structural position, in seq order: "
            "`{stage, seq, side, divergences: [{atom, kind, detail}]}` "
            "(side ∈ both | a_only | b_only)."
        ),
    )
    structural: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structural divergences (unmatched stages) in the T1 flag shape.",
    )
    render: str = Field(
        description="Deterministic, self-describing markdown overlay (trusted-display; no verdicts)."
    )
