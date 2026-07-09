"""Wire shapes for the determinism fingerprint (``docs/design/determinism-fingerprint.md``).

The fingerprint is a measured, accumulating, confidence-labeled record of an
experiment's observed run-to-run spread. This module is the *authoring SoT* for
its wire vocabulary:

* the **envelope** a comparison applied to a key (``EnvelopeApplied``) and the
  **evidence** that weights it (``EnvelopeEvidence``) — the D-verdict-wire
  shapes verify-reproduction stamps onto every receipt key (schema_version 2);
* the caller-authored **evidence demand** a registration matches against
  (``EvidenceDemandSpec`` → ``evidence_demand.input.json``) — the one predicate
  ``state/determinism.py::evidence_meets`` consumes;
* the **sample record** (``DeterminismSampleRecord``, schema_version 1) — the
  D-store ledger line, one per comparison, echoed on verify-reproduction's
  result so a consumer sees the sample the comparison just appended.

Boundary posture (Q1, ``docs/internals/engineering-principles.md``): every field
here is a MECHANISM noun — ``key``, ``lo``, ``hi``, ``rel_spread``, ``n``,
``scale``, ``cluster`` — never a metric NAME or a domain role. The comparator
above measures opaque numbers; naming and judging live with the human. The
``determinism`` boundary test walks these models' property names against the
same forbidden domain-vocabulary set the dossier boundary pins.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict

# ── literals (D-store / D-verdict-wire vocabulary) ───────────────────────────

# Per-key reason a tolerance-class verdict was reached (D-verdict-wire, verbatim).
# ``None`` (on the field) = no envelope/tolerance participated (an exact-class
# key with no spread, or a still-untyped key).
TierReason = Literal[
    "exact",
    "within_evidenced_envelope",
    "within_thin_envelope",
    "outside_thin_envelope",
    "outside_evidenced_envelope",
    "caller_override",
]

# Static structural class of a flattened metric leaf (D-store ``static_class``).
# Only ``float`` is tolerance-class-eligible; everything else compares EXACTLY.
StaticClass = Literal["float", "int", "str", "bool", "shape"]

# Per-key envelope class (D-envelope): ``exact`` = every sample identical on the
# key; ``stochastic`` = nonzero float spread observed, the range is the envelope.
EnvelopeClass = Literal["exact", "stochastic"]

# Where a sample came from (D-store ``source``) and its mechanically-assigned
# scale label — ``canary`` for a double-canary pair, ``main`` for a
# verify-reproduction (a partial reproduction is main-scale with ``partial``).
SampleSource = Literal["double-canary", "verify-reproduction"]
SampleScale = Literal["canary", "main"]

# The comparison's verdict AT APPEND (D-store; D-consume clause 1 — judgment
# always precedes append). Admission joins on it in the store layer.
SampleVerdict = Literal["auto_cleared", "needs_verdict", "mismatch"]


# ── envelope + evidence (D-verdict-wire) ─────────────────────────────────────


class EnvelopeEvidence(BaseModel):
    """The evidence weight behind an envelope — LABELED, never trusted blind.

    Every consumer reads this before trusting the width (design center 2): a
    stochastic envelope at ``n=2`` is WEAK and says so. Order statistics carry no
    distributional claim; this block is how thin-vs-well-evidenced is mechanized
    (``n >= 3`` + scale coverage + cluster coverage), never judged.
    """

    model_config = ConfigDict(extra="forbid", title="determinism envelope evidence")

    n: int = Field(
        ge=0, description="Total CURRENT-identity ADMITTED samples backing the envelope."
    )
    n_full: int = Field(
        ge=0,
        description="Admitted non-partial (full-task) samples — the scale-quality leg min_n_full demands.",
    )
    n_partial: int = Field(ge=0, description="Admitted partial-reproduction samples.")
    scales: list[SampleScale] = Field(
        default_factory=list,
        description="Distinct scale labels observed across the admitted samples (canary / main).",
    )
    clusters: list[str] = Field(
        default_factory=list,
        description="Distinct measuring clusters observed across the admitted samples.",
    )
    same_submission_only: bool = Field(
        description=(
            "True when every backing sample is a same-submission double-canary pair "
            "(one environment observed twice, not two) — a thinness signal."
        ),
    )
    excluded_unadmitted: int = Field(
        default=0,
        ge=0,
        description=(
            "Recorded-but-INADMISSIBLE samples excluded from the envelope (D-consume "
            "no-silent-caps disclosure) — unresolved needs_verdict / unaccepted "
            "mismatch lines that inform the human, never the auto path."
        ),
    )


class EnvelopeApplied(BaseModel):
    """The exact range + evidence weight a comparison judged a key against.

    Recorded on every verdict's receipt key (D-envelope resolution disclosure):
    the guarantee is not "no false positives" but NO UNDISCLOSED RESOLUTION — a
    consumer needing finer resolution refuses the envelope rather than trusting
    it, and every past auto-clear that relied on a too-wide envelope is
    retrospectively identifiable. ``None`` on the field = no envelope applied
    (exact-class key, or caller override).
    """

    model_config = ConfigDict(
        extra="forbid",
        title="determinism envelope applied",
        populate_by_name=True,
        # Emit the wire key ``class`` (the alias), not the Python field name, on
        # every dump — the CLI envelope and the fuzz harness dump WITHOUT
        # by_alias, so the schema (correctly) forbids ``envelope_class``.
        serialize_by_alias=True,
    )

    envelope_class: EnvelopeClass = Field(
        alias="class",
        description="exact (no observed spread) / stochastic (nonzero float spread; range is the envelope).",
    )
    lo: float = Field(
        description="min(observed) over all CURRENT-identity admitted samples (order statistic)."
    )
    hi: float = Field(
        description="max(observed) over all CURRENT-identity admitted samples (order statistic)."
    )
    rel_spread: float = Field(
        ge=0.0,
        description="Derived max relative spread of the observed range — a description, never an extrapolation.",
    )
    evidence: EnvelopeEvidence = Field(
        description="The labeled evidence weight behind this envelope."
    )


# ── the evidence demand (design center 4 / registration seam) ────────────────


class EvidenceDemandSpec(BaseModel):
    """A caller-authored demand on the fingerprint's accumulated evidence.

    The registration kernel (``docs/design/registration-kernel.md``) DEMANDS
    evidence tiers — e.g. "fingerprint at main-scale, n>=3" — and the fingerprint
    exposes ONE pure predicate (``state/determinism.py::evidence_meets``) that
    consumes exactly this shape, counting ADMITTED CURRENT-identity samples only.
    Core matches by identity and counts, never interprets.
    """

    model_config = ConfigDict(extra="forbid", title="determinism evidence demand")

    min_n: int = Field(
        ge=1,
        description="Minimum admitted samples (n_full + n_partial both count) the evidence must carry.",
    )
    min_n_full: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional separate demand on scale-quality — full (non-partial) admitted "
            "samples only, over the n_full leg the evidence block isolates."
        ),
    )
    scales: list[SampleScale] = Field(
        default_factory=list,
        description="Scale labels the evidence must cover (empty = no scale constraint). Plural.",
    )
    clusters: list[str] = Field(
        default_factory=list,
        description="Measuring clusters the evidence must cover (empty = no cluster constraint). Plural.",
    )


# ── the sample record (D-store shape, schema_version 1) ──────────────────────


class SampleIdentity(BaseModel):
    """The experiment IDENTITY every sample carries verbatim (the ``_IDENTITY_FIELDS``
    discipline) — the CURRENT-identity filter compares against these, reading a
    ``tasks_py_sha``-drifted prior STALE."""

    model_config = ConfigDict(extra="forbid", title="determinism sample identity")

    cmd_sha: str = Field(description="Param identity (state/run_sha.py) — the ledger key.")
    tasks_py_sha: str = Field(description="Task-generator code identity.")
    executor: str = Field(description="Executor script identity.")
    data_sha: str | None = Field(
        default=None,
        description=(
            "Data-identity leg (Phase-3 amendment, ruled 0b): the data-manifest sha of "
            "the declared input roots at record time, lifted from the run sidecar's "
            "data_manifest_sha. ADDITIVE + optional — null is 'data identity unknown "
            "(no manifest at record time)', disclosed and never blocking. A sample under "
            "DIFFERENT data is excluded as data drift, never admitted as nondeterminism "
            "evidence; a pre-amendment record (no data_sha) parses byte-identically."
        ),
    )


class SampleKeyDiff(BaseModel):
    """One flattened metric leaf's observed pair in a sample (D-store ``per_key``).

    ``a`` / ``b`` are the two compared VALUES — opaque scalars, never named. The
    diff fields are null for non-numeric leaves (equality-only), present for
    float leaves.
    """

    model_config = ConfigDict(extra="forbid", title="determinism sample per-key diff")

    key: str = Field(
        description="Flattened metric key (opaque path; the comparator never reads its meaning)."
    )
    a: float | int | str | bool | None = Field(
        description="Value observed on side A (opaque scalar)."
    )
    b: float | int | str | bool | None = Field(
        description="Value observed on side B (opaque scalar)."
    )
    abs_diff: float | None = Field(
        default=None, description="|a - b| for numeric leaves; null otherwise."
    )
    rel_diff: float | None = Field(
        default=None, description="|a - b| / max(|a|, |b|) for numeric leaves; null otherwise."
    )
    static_class: StaticClass = Field(
        description="Structural class of this leaf (only float is tolerance-eligible)."
    )


class DeterminismSampleRecord(BaseModel):
    """One append-only ledger line — a single observed run-to-run comparison.

    A valid ``state/attestation.py::validate`` record: the attestation fields
    (``attestor``, ``subject_kind``, ``subject_id``, ``content_sha``) make each
    line bind-lockable, so a spread cannot be asserted into existence. The
    subject is the experiment IDENTITY, not one run — original and reproduction
    samples accumulate to the SAME ledger, keyed on ``subject_id`` (cmd_sha).
    """

    model_config = ConfigDict(extra="forbid", title="determinism fingerprint sample record")

    ts: str = Field(description="ISO-8601 UTC append timestamp.")
    schema_version: Literal[1] = Field(
        default=1, description="Sample-record schema version (bump on shape change)."
    )
    attestor: Literal["code"] = Field(
        default="code", description="Always code — a sample is a code attestation."
    )
    subject_kind: Literal["determinism-fingerprint"] = Field(
        default="determinism-fingerprint", description="Attestation subject kind."
    )
    subject_id: str = Field(description="The experiment identity this sample attests (cmd_sha).")
    content_sha: str = Field(
        description=(
            "Canonical SHA over the two COMPARED on-disk payloads (harness-contract "
            "canonicalization) — the bind recompute key and the admission join key."
        ),
    )
    identity: SampleIdentity = Field(description="Full identity fields, lifted verbatim.")
    source: SampleSource = Field(
        description="double-canary (n=2 prior) or verify-reproduction (accreting sample)."
    )
    run_ids: list[RunIdStrict] = Field(description="The two runs compared; [a, b].")
    cluster: str = Field(
        description="The measuring cluster (cross-cluster spread is an env-sensitivity finding)."
    )
    scale: SampleScale = Field(
        description="canary (double-canary) or main (verify-reproduction) — never judged."
    )
    verdict: SampleVerdict = Field(
        description="The comparison's verdict AT APPEND (judgment precedes append)."
    )
    same_submission: bool = Field(
        default=False,
        description="True for a double-canary pair (one environment observed twice) — a correlated-sample label.",
    )
    partial: bool = Field(
        default=False, description="True for a partial-reproduction sample (subset of tasks)."
    )
    task_indices: list[int] | None = Field(
        default=None,
        description="The exact task indices compared on a partial sample; null for a full sample.",
    )
    per_key: list[SampleKeyDiff] = Field(
        default_factory=list, description="Per-flattened-key observed pairs for this comparison."
    )
