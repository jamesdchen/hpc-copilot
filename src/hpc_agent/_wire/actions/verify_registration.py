"""Pydantic models for the ``verify-registration`` query's spec + result.

``verify-registration`` is the read-only consumer seat of the registration
kernel (``docs/design/registration-kernel.md`` R8): given a
``registration_id`` (or a ``run_id`` to find the registrations naming it), it
recomputes the prerequisite chain and the live dossier signature AT READ TIME
and REPORTS the reduced status, the per-leg detail, and the code-rendered
markdown brief whose canonical-JSON sha is the ``view_sha`` a subsequent
sign-off must carry. It never blocks — the deployment refusal lives
caller-side (R8: "core does not own the deploy boundary").

Boundary posture (``docs/internals/engineering-principles.md`` Q1): every
field name here is a MECHANISM noun — a store, a leg, a sha, a count. Core
never learns what is being registered, what a field slug means, or what
"ready to deploy" means. The only vocabularies core owns on this wire are the
status set (R7) and ``PrerequisiteKind`` (the closed mechanism-noun set,
mirrored from ``state/registration.py::PREREQUISITE_KINDS`` — T1 — and pinned
equal by the T9 contract suite). Field slugs, ``subject_id``s and evidence
notes are opaque caller data: counted, echoed, never interpreted. The wire
carries no ``_FORBIDDEN_FIELD_NAMES`` member (the dossier-boundary walk,
mirrored in T9).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import RunIdStrict

# ── mechanism vocabularies (core-owned) ────────────────────────────────────

# The reduced registration status (R7). ``current`` requires the newest record
# to be a registration whose chain AND live dossier signature both still hold;
# ``stale`` names every failing leg; ``revoked`` = a newest ``registration-revoke``
# record; ``superseded`` = an older record a newer registration displaced;
# ``absent`` = no registration record names the subject.
RegistrationStatus = Literal["current", "stale", "revoked", "superseded", "absent"]

# The per-leg currency of a single template leg or prerequisite slot re-evaluated
# at read time. ``current`` = the leg's route-through checker (or the template
# raw-bytes sha) reads current and the recorded sha still matches; ``stale`` =
# the evidence moved since registration (recorded-vs-recomputed sha differ, or the
# checker no longer reads current); ``absent`` = the leg's evidence could not be
# found at all (e.g. a missing receipt ledger — an ordinary shortfall, R4).
LegStatus = Literal["current", "stale", "absent"]

# Template-drift finding (R5): a distinct, always-disclosed report, never a
# silent gate. Template drift does NOT retroactively revoke a registration; a
# consumer requiring re-registration under new standards reads this leg.
TemplateStatus = Literal["current", "stale"]

# The CLOSED set of prerequisite-chain kinds (R3). Mechanism nouns only — a
# store/mechanism each chain entry dispatches to for a currency recompute, never
# a domain word. Mirrors ``state/registration.py::PREREQUISITE_KINDS`` (T1); the
# T9 contract suite pins the two equal so adding a kind is a reviewed vocabulary
# change in ONE place. ``pack-receipt`` is reserved (lands with the domain-pack
# substrate); until then T4's checker refuses it loudly (never a silent pass).
PrerequisiteKind = Literal[
    "notebook-audit",
    "reproduction",
    "scope-budget",
    "pack-receipt",
    "attestation",
]


# ── the prerequisite-chain input shape (R3/R4) ─────────────────────────────


class PrerequisiteRequires(BaseModel):
    """An evidence-tier floor a chain entry MAY declare (R4).

    The load-bearing case is the ``reproduction`` kind against the determinism
    fingerprint: registration is the seat that can demand main-scale evidence
    before "reproducible" counts. These four keys are the fingerprint's exact
    demand vocabulary (``docs/design/registration-kernel.md`` R4, one vocabulary
    across both docs). Core checks each by IDENTITY / COMPARISON / COUNTING
    against evidence the repro machinery already recorded — never by a predicate
    core evaluates:

    * ``min_n`` — ``n >= min_n`` where ``n`` counts full + partial samples both.
    * ``min_n_full`` — the scale-quality floor: ``n_full >= min_n_full`` over the
      non-partial samples the fingerprint's evidence block isolates.
    * ``scales`` / ``clusters`` — every named label must be present in the
      recorded set (identity over opaque labels; core never learns their
      meaning).

    ``extra="forbid"``: an unknown ``requires`` key for a kind is a LOUD refusal
    (``errors.SpecInvalid`` at the core gate, R4 — an opted-in requirement core
    cannot check must never silently pass). The ``attestation`` kind accepts NO
    ``requires`` at all; ``notebook-audit`` needs none. ``scope-budget`` /
    ``pack-receipt`` requirement shapes land with their T4 checkers.
    """

    model_config = ConfigDict(extra="forbid", title="prerequisite evidence-tier floor")

    min_n: int | None = Field(
        default=None,
        ge=1,
        description="Minimum sample count (full + partial both) the evidence must reach.",
    )
    min_n_full: int | None = Field(
        default=None,
        ge=1,
        description="Minimum count of FULL (non-partial) samples — the scale-quality floor.",
    )
    scales: list[str] | None = Field(
        default=None,
        description="Scale labels that must each be present in the recorded scales set (opaque labels).",
    )
    clusters: list[str] | None = Field(
        default=None,
        description="Cluster labels that must each be present in the recorded clusters set (opaque labels).",
    )


class ChainEntry(BaseModel):
    """A single prerequisite-chain entry — the full-address naming shape (R3).

    A registration NAMES its required prior attestations as full addresses (a
    bare slug was rejected: a slug cannot be mechanically checked for currency).
    Each entry lets the append/verify gate dispatch to the ONE existing checker
    for its ``kind`` and compare the asserted ``content_sha`` against the
    checker's recomputed answer — the chain is a list of recompute locks, not a
    list of claims.

    ``subject_id`` and ``requires`` are opaque to the wire: ``subject_id`` is an
    audit_id / run_id / scope tag / pack slot (core never parses it), and the
    per-kind ``requires`` vocabulary is validated core-side (T4) — so ``requires``
    is carried as an open mapping here (the ``export_dossier.manifest`` precedent:
    the wire does not freeze an ops-layer vocabulary). :class:`PrerequisiteRequires`
    documents the ``reproduction`` kind's floor shape for typed consumers.
    """

    model_config = ConfigDict(extra="forbid", title="prerequisite chain entry")

    slot: str = Field(
        min_length=1,
        description="Caller-authored slug naming this chain slot (opaque; counted, never read for meaning).",
    )
    kind: PrerequisiteKind = Field(
        description="The mechanism kind this entry dispatches to for its currency recompute (closed set).",
    )
    subject_id: str = Field(
        min_length=1,
        description="Opaque address the kind's checker resolves (audit_id / run_id / scope tag / pack slot).",
    )
    content_sha: str = Field(
        min_length=1,
        description="The sha the prerequisite was recorded current at; recomputed and compared by the gate.",
    )
    requires: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional evidence-tier floor for this kind (see PrerequisiteRequires for the "
            "reproduction shape). Carried as an open mapping; per-kind key validation is "
            "core-side — an unknown key is a loud SpecInvalid, never a silent pass."
        ),
    )


# ── the query spec ─────────────────────────────────────────────────────────


class VerifyRegistrationSpec(BaseModel):
    """Input spec for ``verify-registration`` — an either-or address.

    EXACTLY ONE of ``registration_id`` / ``run_id`` is supplied:

    * ``registration_id`` — verify that one registration directly (the newest
      record under that id wins; R7).
    * ``run_id`` — find the registration(s) naming that run and report the
      resolved one.

    Supplying both, or neither, is refused. Both are ``RunIdStrict`` — a
    caller-authored filesystem-safe slug: ``registration_id`` becomes a path
    segment under ``.hpc/registrations/`` (R9), never core-invented.
    """

    model_config = ConfigDict(extra="forbid", title="verify-registration input spec")

    registration_id: RunIdStrict | None = Field(
        default=None,
        description="Verify this registration directly. Mutually exclusive with run_id.",
    )
    run_id: RunIdStrict | None = Field(
        default=None,
        description="Find the registration(s) naming this run. Mutually exclusive with registration_id.",
    )

    @model_validator(mode="after")
    def _exactly_one_address(self) -> VerifyRegistrationSpec:
        supplied = [x for x in (self.registration_id, self.run_id) if x is not None]
        if len(supplied) != 1:
            raise ValueError(
                "verify-registration takes EXACTLY ONE of registration_id / run_id "
                f"(got {len(supplied)}): name the registration directly, or a run to "
                "find the registrations that name it."
            )
        return self


# ── the per-leg result detail (R8) ─────────────────────────────────────────


class DossierLeg(BaseModel):
    """The dossier-subject leg: recorded-vs-recomputed signature + drift (R8).

    The registration's subject is the sealed dossier, bound by its
    ``bundle_sha256`` (R2). ``recomputed_sha`` is the live dry re-gather at read
    time; ``recorded_sha`` is what the registration bound. When they differ a
    sealed store moved after export — the registration reads ``stale`` and
    ``drifted_stores`` names the stores whose content moved.
    """

    model_config = ConfigDict(extra="forbid", title="dossier signature leg")

    recorded_sha: str = Field(description="The dossier bundle_sha256 the registration bound.")
    recomputed_sha: str = Field(
        description="The dossier bundle_sha256 re-gathered live from the stores at read time.",
    )
    drifted_stores: list[str] = Field(
        default_factory=list,
        description="Source-store names whose content moved since the dossier was sealed (empty when current).",
    )


class TemplateLeg(BaseModel):
    """The template-drift finding (R5) — always disclosed, never a silent gate.

    Template drift does NOT retroactively revoke a registration (the subject is
    the dossier; the template's raw-bytes sha is recorded on the record). This
    leg REPORTS ``current | stale`` so a consumer can require re-registration
    under new standards — the drift is disclosed, the consumer decides.
    """

    model_config = ConfigDict(extra="forbid", title="template drift leg")

    status: TemplateStatus = Field(
        description="current = the on-disk template matches the recorded sha; stale = it drifted.",
    )
    recorded_sha: str = Field(description="The template raw-bytes sha the registration recorded.")
    recomputed_sha: str = Field(
        description="The template file's raw-bytes sha on disk at read time."
    )


class PrerequisiteLeg(BaseModel):
    """One prerequisite slot's currency re-evaluated at read time (R8).

    Per-slot: the kind's CURRENT condition re-checked. A moved evidence sha is
    reported as the ``recorded_sha`` / ``recomputed_sha`` pair (R3 drift-log).
    ``evidence_note`` echoes what filled the slot — for the generic
    ``attestation`` kind, the satisfying record's ``{block, attestor}`` verbatim
    (R3 disclosure), so an agent-authored record that fills a slot is visible in
    the brief, never silent.
    """

    model_config = ConfigDict(extra="forbid", title="prerequisite slot leg")

    slot: str = Field(min_length=1, description="The chain slot slug (opaque caller data).")
    kind: PrerequisiteKind = Field(description="The mechanism kind this slot was checked through.")
    status: LegStatus = Field(
        description="current | stale | absent — the slot's currency at read time."
    )
    recorded_sha: str | None = Field(
        default=None,
        description="The content_sha the entry recorded (None when the evidence is absent).",
    )
    recomputed_sha: str | None = Field(
        default=None,
        description="The sha the kind's checker recomputed at read time (None when absent).",
    )
    evidence_note: str = Field(
        default="",
        description=(
            "What filled the slot, echoed verbatim (e.g. the attestation kind's "
            "{block, attestor}); or the shortfall cause when absent. Opaque, echoed, never parsed."
        ),
    )


class FieldsBlock(BaseModel):
    """Template-field completeness by COUNTING (R5) — slugs opaque, never read.

    Every declared field slug must have a non-empty value in the registration's
    ``fields`` (values opaque, never interpreted). This leg reports the declared
    slugs, which are present, and which are missing.
    """

    model_config = ConfigDict(extra="forbid", title="template fields completeness")

    declared: list[str] = Field(
        default_factory=list,
        description="Field slugs the template declared (opaque caller data).",
    )
    present: list[str] = Field(
        default_factory=list,
        description="Declared slugs that carry a non-empty value in the registration.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="Declared slugs with no non-empty value (empty when the registration is complete).",
    )


class VerifyRegistrationResult(BaseModel):
    """Result of a ``verify-registration`` read — reduced status + per-leg detail.

    A REPORT, never a gate: a non-``current`` status is a successful run (the
    finding IS the feature). The deployment refusal is wired caller-side against
    ``status`` (R8). ``view_sha`` is the canonical-JSON sha of ``brief`` — the
    witness a subsequent registration sign-off must carry (R6), recomputed by
    the gate from the same deterministic projection.
    """

    model_config = ConfigDict(extra="forbid", title="verify-registration output")

    status: RegistrationStatus = Field(
        description="The reduced registration status (R7): current | stale | revoked | superseded | absent.",
    )
    registration_id: str | None = Field(
        default=None,
        description="The resolved registration's id (None when status is absent — no record found).",
    )
    registered_at: str | None = Field(
        default=None,
        description="Timestamp the resolved registration was recorded (None when absent).",
    )
    dossier: DossierLeg | None = Field(
        default=None,
        description="The dossier-signature leg (None when no registration was resolved).",
    )
    template: TemplateLeg | None = Field(
        default=None,
        description="The template-drift finding (None when no registration was resolved).",
    )
    prerequisites: list[PrerequisiteLeg] = Field(
        default_factory=list,
        description="Per-slot currency detail for every chain entry (empty when absent).",
    )
    fields: FieldsBlock = Field(
        default_factory=FieldsBlock,
        description="Template-field completeness (declared / present / missing slugs).",
    )
    brief: str = Field(
        default="",
        description="The code-rendered markdown brief the human reviews; its canonical-JSON sha is view_sha.",
    )
    view_sha: str = Field(
        default="",
        description="Canonical-JSON sha of the rendered brief — the view witness a sign-off must carry (R6).",
    )
