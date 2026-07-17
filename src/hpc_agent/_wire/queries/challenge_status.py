"""Pydantic models for the ``challenge-status`` query (challenge-attestation T2).

The challenge attestation (``docs/design/challenge-attestation.md``) is the
missing DISSENT object: a human-authored, evidence-bound, sha-targeted
attestation against a committed record — standing (never consumed), disclosed
wherever the challenged record is cited, never blocking. ``challenge-status``
(C-verb) is the ONE read-only projection over that machinery: ``verb="query"``,
``side_effects=[]``, ``idempotent=True``, ``requires_ssh=False`` (the
``verify-registration`` / ``notebook-status`` posture). It answers either

* **the thread view** — keyed by a ``challenge_id``: the filing, its verdict or
  withdrawal, the target it attacks and the evidence it rests on; OR
* **the "what stands against this record?" view** — keyed by a target address
  (``content_sha`` alone, OR the ``{subject_kind, subject_id}`` pair): every
  standing challenge whose target matches.

Both route through the ONE collector ``state/challenges.py::standing_challenges``
(C-reduce; T3 composes it) — this wire never re-collects. The result reports the
reduced per-challenge status (``open|upheld|dismissed|withdrawn|superseded`` —
C-shape, the SAME five T1 reduces to), the target's re-resolution
(``found-current|found-superseded|unresolvable``), per-citation read-time
disclosure (``verified`` / unresolvable — the E read posture: disclose, never
refuse), the ``contested`` counts block (C-status), and the code-rendered
markdown brief whose canonical-JSON sha is the ``view_sha`` a subsequent verdict
may carry (the v1.6 deterministic-render → gate-recomputable rule).

Boundary posture (``docs/internals/engineering-principles.md`` Q1; the
challenge-attestation Agnosticism rows): every field name here is a MECHANISM
noun — a count, a date, a sha, a status literal, an opaque id. ``grounds`` and
``reasoning`` are opaque caller prose (stored, echoed VERBATIM, never parsed —
the ``finding`` discipline). The wire carries NO ``_FORBIDDEN_FIELD_NAMES``
member (the dossier-boundary walk, mirrored in the T2 wire test).

The helper item models below carry NON-``Spec``/``Result`` suffixes so the
``SCHEMA_REGISTRY`` auto-walk inlines them as ``$defs`` rather than emitting a
standalone schema file each — only ``ChallengeStatusSpec`` and
``ChallengeStatusResult`` are top-level wire schemas.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import RunIdStrict

# ── mechanism vocabularies (core-owned) ─────────────────────────────────────

# The reduced per-challenge status (C-shape; routes through
# ``state/attestation.py::reduce`` + verdict/withdraw winner-selection + the
# computed-superseded re-resolution — T1's reduction). ``open`` = filed, no
# terminal record and target still current; ``upheld`` / ``dismissed`` = a human
# verdict resolved it; ``withdrawn`` = the challenger withdrew; ``superseded`` =
# COMPUTED — the target's subject no longer carries the challenged content_sha,
# so the attack reads superseded regardless of verdict records (drift-for-free).
# These are the SAME FIVE ``state/challenges.py`` (T1) reduces to; the contract
# suite (T9) pins the two equal so the vocabulary lives in one place.
ChallengeStatus = Literal["open", "upheld", "dismissed", "withdrawn", "superseded"]

# The target's re-resolution at READ (C-verb): ``found-current`` = the target's
# subject still carries the challenged content_sha (newest); ``found-superseded``
# = the subject exists but its newest content_sha has moved past the challenged
# one; ``unresolvable`` = the target's resolver cannot address the record here
# (evidence legitimately moves — disclosed, never refused, the E read posture).
TargetResolution = Literal["found-current", "found-superseded", "unresolvable"]

# The CLOSED set of resolver-dispatch kinds (the evidence-memory ``CITATION_KINDS``
# — one closed vocabulary, one resolver table, never a parallel copy). Used for
# BOTH the challenge's target address and each cited evidence line. Mirrors
# ``state/evidence.py::CITATION_KINDS``; the contract suite pins the two equal so
# adding a kind is a reviewed vocabulary change in ONE place.
# MIRROR: hpc_agent.state.evidence::CITATION_KINDS pinned-by tests/contracts/test_challenge_boundary.py::test_wire_citation_kind_equals_citation_kinds
CitationKind = Literal["dossier", "run", "fingerprint", "attestation", "recipe"]

# The verdict a resolution record carries (C4). ``upheld`` = the refutation
# becomes a dated record; ``dismissed`` = waved away WITH typed reasoning
# (dismissal is effortful by construction). Withdrawal is NOT a verdict — it is
# the challenger retracting, reduced to the ``withdrawn`` status, no verdict line.
VerdictOutcome = Literal["upheld", "dismissed"]


# ── the query spec ──────────────────────────────────────────────────────────


class ChallengeStatusSpec(BaseModel):
    """Input spec for ``challenge-status`` — the ONE read-only challenge query.

    EXACTLY ONE addressing mode (C-verb, the honest reading — recorded in the T2
    drift note):

    1. ``challenge_id`` — the THREAD view: filing + verdict/withdrawal + target +
       evidence for one challenge.
    2. ``content_sha`` — the TARGET view keyed by the exact challenged sha (the
       full address's discriminator; C-reduce matches on it exactly).
    3. the ``{subject_kind, subject_id}`` PAIR — the TARGET view keyed by subject
       identity (every standing challenge against that subject, across shas).

    The three map onto ``standing_challenges``'s ``content_sha`` / ``subject_kind``
    / ``subject_id`` filter params (C-reduce). They are DISTINCT modes and never
    combine: a ``challenge_id`` alongside a target address, a ``content_sha`` mixed
    with a subject, or no address at all, each refuses (the ``verify-registration``
    EXACTLY-ONE posture). The subject pair is ATOMIC — a lone ``subject_kind`` or
    lone ``subject_id`` addresses nothing checkable (the R3 full-address
    discipline) and refuses. ``fleet`` widens any mode across every journaled
    experiment.
    """

    model_config = ConfigDict(extra="forbid", title="challenge-status input spec")

    challenge_id: RunIdStrict | None = Field(
        default=None,
        description=(
            "The caller-authored challenge slug to view as a thread (filing + "
            "verdict/withdrawal). Mode 1; null when addressing by target."
        ),
    )
    content_sha: str | None = Field(
        default=None,
        description=(
            "The exact challenged content_sha to find standing challenges against "
            "(the full address's discriminator; matched exactly). Mode 2; null "
            "otherwise."
        ),
    )
    subject_kind: str | None = Field(
        default=None,
        description=(
            "The challenged record's subject_kind (opaque). Mode 3 — REQUIRED with "
            "subject_id (the pair is atomic); null otherwise."
        ),
    )
    subject_id: str | None = Field(
        default=None,
        description=(
            "The challenged record's subject_id (opaque). Mode 3 — REQUIRED with "
            "subject_kind (the pair is atomic); null otherwise."
        ),
    )
    fleet: bool = Field(
        default=False,
        description=(
            "When False (default), scope is the single experiment_dir. When True, "
            "run the identical challenge walk over every journaled experiment "
            "(the evidence-brief fleet posture); torn namespaces are skipped and "
            "counted in 'skipped'."
        ),
    )

    @model_validator(mode="after")
    def _requires_exactly_one_address(self) -> ChallengeStatusSpec:
        subject_partial = self.subject_kind is not None or self.subject_id is not None
        subject_complete = self.subject_kind is not None and self.subject_id is not None
        modes = [
            self.challenge_id is not None,
            self.content_sha is not None,
            subject_partial,
        ]
        if sum(modes) != 1:
            raise ValueError(
                "challenge-status needs EXACTLY ONE addressing mode: a challenge_id "
                "(the thread view), a content_sha, or the {subject_kind, subject_id} "
                "pair (a target view). A bare slug addresses nothing checkable — the "
                "R3 full-address discipline."
            )
        if subject_partial and not subject_complete:
            raise ValueError(
                "subject_kind and subject_id are an ATOMIC pair — name BOTH or "
                "neither: a lone subject half addresses nothing the machine can "
                "resolve (the R3 full-address discipline)."
            )
        return self


# ── the per-item result detail (mechanism-nouned; inline $defs) ─────────────


class ChallengeTarget(BaseModel):
    """The committed record a challenge attacks — citation-shaped + the R3 address.

    The ``kind`` dispatches target resolution through the SAME ``CITATION_KINDS``
    resolver table evidence-memory owns (one definition). ``subject_kind`` /
    ``subject_id`` / ``content_sha`` are the full address the filing gate verified
    exists as a committed record at exactly this sha — echoed here VERBATIM;
    ``subject_kind`` and ``subject_id`` are opaque caller identities, never parsed.
    """

    model_config = ConfigDict(extra="forbid", title="challenge target address")

    kind: CitationKind = Field(
        description="The resolver-dispatch kind (closed set) — which store resolves the target."
    )
    subject_kind: str = Field(
        description="The challenged record's subject_kind (opaque caller identity; echoed, never parsed)."
    )
    subject_id: str = Field(
        description="The challenged record's subject_id (opaque caller identity; echoed, never parsed)."
    )
    content_sha: str = Field(
        description="The exact challenged content_sha (the full address's discriminator)."
    )


class ChallengeVerdict(BaseModel):
    """The human verdict that resolved a challenge (C4) — present only when ruled.

    ``verdict`` is ``upheld`` | ``dismissed``; ``reasoning`` is the mandatory,
    opaque, verbatim-echoed dissent-resolution prose (never parsed); ``ts`` dates
    the ruling. Absent on an ``open`` or ``withdrawn`` challenge (the
    emitted-only-when-present precedent). A verdict on a SINCE-superseded
    challenge remains disclosed here even as ``status`` reads ``superseded``
    (both reported; superseded wins the headline — C-reduce).
    """

    model_config = ConfigDict(extra="forbid", title="challenge verdict record")

    verdict: VerdictOutcome = Field(
        description="upheld | dismissed — the human's resolution of the challenge."
    )
    reasoning: str = Field(
        description="The human's free-text resolution rationale — opaque, echoed verbatim, never parsed."
    )
    ts: str = Field(description="The ruling's date (the verdict record's ts).")


class ChallengeEntry(BaseModel):
    """One reduced challenge — its status, target, grounds, and (if ruled) verdict.

    The lead of both views. ``status`` is the reduced per-challenge status;
    ``resolution`` is the target's read-time re-resolution (disclosed, never
    refused); ``grounds`` is the challenger's opaque free-text dissent (echoed
    verbatim, never parsed); ``verdict`` rides only when a ruling exists.
    """

    model_config = ConfigDict(extra="forbid", title="challenge entry")

    challenge_id: str = Field(
        description="The challenge's caller-authored slug (opaque path segment)."
    )
    status: ChallengeStatus = Field(
        description="open | upheld | dismissed | withdrawn | superseded — the reduced status.",
    )
    filed_at: str = Field(
        description="The filing record's ts — every challenge dated (the item ages while open)."
    )
    target: ChallengeTarget = Field(
        description="The committed record this challenge attacks (address + resolver kind)."
    )
    resolution: TargetResolution = Field(
        description="found-current | found-superseded | unresolvable — the target re-resolved at read.",
    )
    grounds: str = Field(
        description="The challenger's free-text dissent — opaque, echoed verbatim, never interpreted.",
    )
    verdict: ChallengeVerdict | None = Field(
        default=None,
        description="The human verdict record when the challenge was ruled; null while open/withdrawn.",
    )


class CitationStatusLine(BaseModel):
    """Per-citation read-time re-resolution disclosure (the E read posture).

    A challenge MUST cite evidence shas at filing (they resolve LIVE or the append
    gate refuses). Evidence legitimately moves afterward (archived, re-exported,
    wiped); the read side re-resolves each citation and reports ``verified`` per
    line — the challenge stays a truthful dated record; the drift is disclosed,
    the reader decides (the append gate is the deliberate refuse exception, never
    run here). ``challenge_id`` scopes the line to its challenge.
    """

    model_config = ConfigDict(extra="forbid", title="challenge citation-status line")

    challenge_id: str = Field(
        description="The challenge this cited evidence belongs to (opaque slug)."
    )
    kind: CitationKind = Field(
        description="The citation's resolver-dispatch kind (closed set) — its one resolver."
    )
    ref: str = Field(
        description="The opaque citation ref (run_id / cmd_sha key / dossier path); echoed, never parsed."
    )
    sha: str = Field(description="The full sha the challenge recorded for this citation.")
    verified: bool = Field(
        description="True = re-resolved and the sha still matches here; False = unresolvable/mismatched at read.",
    )


class ContestedCounts(BaseModel):
    """The ``contested`` projection every disclosure seat carries (C-status).

    Counts and identities, never a severity score (the attention-queue no-urgency
    rule; a merit score is core grading dissent). Parallel to — never folded into
    — any status vocabulary: a target can be ``current`` AND ``contested``. A
    seat whose target has ALL-ZERO counts omits the block entirely (the
    emitted-only-when-present precedent); this model is the shape when present.
    """

    model_config = ConfigDict(extra="forbid", title="challenge contested counts")

    open: int = Field(
        default=0, description="Count of open (unresolved, un-superseded) challenges."
    )
    upheld: int = Field(default=0, description="Count of upheld challenges.")
    dismissed: int = Field(default=0, description="Count of dismissed challenges.")
    withdrawn: int = Field(default=0, description="Count of withdrawn challenges.")
    superseded: int = Field(
        default=0, description="Count of challenges the target's subject has moved past (computed)."
    )
    challenge_ids: list[str] = Field(
        default_factory=list,
        description="The challenge slugs counted here (identities, never a ranking); opaque.",
    )


class SkippedNamespace(BaseModel):
    """One namespace skipped during fleet collection, with the reason (fail-open).

    A wiped repo, an unreadable/torn ``repo.json``, or an absent store must never
    crash the read — it is skipped silently and counted here (the evidence-memory
    / attention-queue ``SkippedNamespace`` posture, mirrored).
    """

    model_config = ConfigDict(extra="forbid", title="challenge skipped namespace")

    ref: str = Field(description="The repo_hash / namespace id that was skipped.")
    reason: str = Field(description="Why it was skipped (unreadable, torn, absent).")


# ── the result model ────────────────────────────────────────────────────────


class ChallengeStatusResult(BaseModel):
    """Shape of the ``data`` field on a ``challenge-status`` envelope (C-verb).

    The reduced per-challenge statuses (both views), per-citation read-time
    disclosure, the ``contested`` counts block, and the fleet skip accounting —
    plus the ``render`` markdown the agent relays VERBATIM and the ``view_sha``
    (the canonical-JSON sha of the deterministic brief) a subsequent verdict may
    carry so the gate can RECOMPUTE what-they-saw (the v1.6 rule). ``computed_at``
    dates the whole projection.
    """

    model_config = ConfigDict(extra="forbid", title="challenge-status output data")

    computed_at: str = Field(
        description="The instant the projection was computed against (ISO-8601 UTC)."
    )
    challenges: list[ChallengeEntry] = Field(
        default_factory=list,
        description="The reduced challenges (thread: one; target view: all matching) — newest filed first.",
    )
    citations_status: list[CitationStatusLine] = Field(
        default_factory=list,
        description="Per-citation read-time re-resolution disclosure (verified / unresolvable).",
    )
    contested: ContestedCounts = Field(
        default_factory=ContestedCounts,
        description="The contested counts across the matched challenges (counts + identities).",
    )
    skipped: list[SkippedNamespace] = Field(
        default_factory=list,
        description="Namespaces skipped during fleet collection (fail-open accounting).",
    )
    render: str = Field(
        description="The deterministic markdown brief — relayed to the human verbatim."
    )
    view_sha: str = Field(
        description="Canonical-JSON sha of the rendered brief — the view a later verdict may bind and the gate recomputes.",
    )
