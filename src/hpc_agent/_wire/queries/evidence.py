"""Pydantic models for the ``evidence-brief`` and ``evidence-period`` queries.

Evidence memory (``docs/design/evidence-memory.md``) answers the cross-experiment
question — "what have we tested under tag X, when, with what envelopes and what
verdicts?" — as a PROJECTION over sealed records (conclusions, scope/look
ledgers, campaign journals, run sidecars, fingerprint ledgers), never a
narrative anyone authored after the fact. Two read-only ``verb="query"``
projections share the one collector (``state/evidence.py::collect_evidence``):

* **``evidence-brief``** — the POINT query (primary, E5): keyed by scope
  ``tags`` and/or a ``lineage`` run_id, a code-rendered digest sized for
  embedding in a greenlight/audit-prelude brief. An unkeyed point query is the
  recorded browse NON-GOAL, refused by the spec's model_validator.
* **``evidence-period``** — the WINDOW projection (E5): a ``since``/``until``
  timeline over the SAME collector, ending with the unconcluded-campaigns list —
  the place the conclusion loop closes.

Boundary posture (``docs/internals/engineering-principles.md`` Q1; the E-render
enforcement rows): every field name here is a MECHANISM noun — a count, a date,
a sha, a tag slug, a verbatim fingerprint-evidence label. Core never interprets
what a tag means or what a ``finding`` says (both opaque, echoed, never parsed).
The wire carries no ``_FORBIDDEN_FIELD_NAMES`` member (the dossier-boundary
walk, mirrored in the T2 wire test and the T11 contract suite). ``render`` rides
the result for verbatim relay (the ``AttentionQueueResult`` posture); the
digest is code-composed with no urgency, recommendation, or interpretation
prose (the queue's D6 rule).

The helper item models below carry NON-``Spec``/``Result`` suffixes so the
``SCHEMA_REGISTRY`` auto-walk inlines them as ``$defs`` rather than emitting a
standalone schema file each — only the two ``*Spec`` and two ``*Result`` models
are top-level wire schemas.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import RunIdStrict

# ── mechanism vocabularies (core-owned) ────────────────────────────────────

# The reduced conclusion status per subject (E-shape reduction; routes through
# ``state/attestation.py::reduce`` + revoke/supersession winner-selection).
# ``current`` = the newest live conclusion; ``superseded`` = displaced by a
# newer conclusion under the same id; ``revoked`` = a newest conclusion-revoke
# record; ``absent`` = no conclusion names the subject. Mirrors T1's reduction
# vocabulary (``state/evidence.py``).
ConclusionStatus = Literal["current", "superseded", "revoked", "absent"]

# The CLOSED set of citation kinds (E-shape ``CITATION_KINDS``), each naming the
# ONE existing resolver the read side re-resolves against. Mechanism nouns only.
# Mirrors ``state/evidence.py::CITATION_KINDS`` (T1); the T11 contract suite pins
# the two equal so adding a kind is a reviewed vocabulary change in ONE place.
CitationKind = Literal["dossier", "run", "fingerprint", "attestation"]

# Cache disposition for this read (E-cache, the ``describe_cache`` posture):
# ``hit`` = served from the content-keyed cache; ``miss`` = recomputed and
# cached; ``disabled`` = ``HPC_NO_EVIDENCE_CACHE`` opt-out (or an I/O
# fall-through). Disclosed on every result — the index is derived and disposable.
CacheDisposition = Literal["hit", "miss", "disabled"]


# ── the query specs ─────────────────────────────────────────────────────────


class EvidenceBriefSpec(BaseModel):
    """Input spec for ``evidence-brief`` — the POINT query (E5, primary).

    Keyed by ``tags`` (scope-tag membership) AND/OR ``lineage`` (a ``run_id``
    whose command-identity chain selects code-identical work — the always-present
    fallback that needs no human to have tagged anything). At LEAST ONE key is
    required: an unkeyed point query is the recorded browse NON-GOAL (the agent
    is the browser; there is no faceted explorer). Both may be given; the union
    is disclosed per-source in the result.
    """

    model_config = ConfigDict(extra="forbid", title="evidence-brief input spec")

    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Scope-tag slugs to select by (membership; a record matches when it "
            "carries any queried tag OR a current conclusion retro-indexes it "
            "under one). Opaque caller data — core never interprets a tag's meaning."
        ),
    )
    lineage: RunIdStrict | None = Field(
        default=None,
        description=(
            "A run_id whose lineage-chain + cmd_sha select by CODE IDENTITY — the "
            "tag-free fallback. Null when the query is tag-keyed only."
        ),
    )
    as_of: str | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 timestamp: the collector includes only records with "
            "ts <= as_of, so the digest is 'what was known as of that date'. A "
            "timestamp, never a named period ('2025H1' is caller vocabulary)."
        ),
    )
    fleet: bool = Field(
        default=False,
        description=(
            "When False (default), scope is the single experiment_dir. When True, "
            "run the identical per-namespace walk over every experiment this "
            "machine has journaled (glob '*/repo.json' under the journal home); a "
            "torn/unreadable namespace is skipped and counted in 'skipped'."
        ),
    )

    @model_validator(mode="after")
    def _requires_a_key(self) -> EvidenceBriefSpec:
        if not self.tags and self.lineage is None:
            raise ValueError(
                "evidence-brief needs AT LEAST ONE of tags / lineage: an unkeyed "
                "point query is the recorded browse non-goal (the agent is the "
                "browser). Name scope tags, or a run_id to key by code identity."
            )
        return self


class EvidencePeriodSpec(BaseModel):
    """Input spec for ``evidence-period`` — the WINDOW projection (E5).

    A time-window digest over the SAME collector, ending with the
    unconcluded-campaigns list. ``since`` is required (a window has a start);
    ``until`` defaults to open-ended (up to now). ``tags`` optionally narrows the
    window to matching scope tags; an empty ``tags`` is the whole-window view (a
    period is inherently time-keyed, so no at-least-one-key rule applies here).
    """

    model_config = ConfigDict(extra="forbid", title="evidence-period input spec")

    since: str = Field(
        description="ISO-8601 window start: only records with ts >= since are projected.",
    )
    until: str | None = Field(
        default=None,
        description="ISO-8601 window end (only records with ts <= until); null = open-ended (up to now).",
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Optional scope-tag slugs narrowing the window; empty = the whole "
            "window. Opaque caller data — core never interprets a tag's meaning."
        ),
    )
    fleet: bool = Field(
        default=False,
        description=(
            "When True, run the identical per-namespace walk over every journaled "
            "experiment (the evidence-brief fleet posture); torn namespaces are "
            "skipped and counted."
        ),
    )


# ── the per-item result detail (mechanism-nouned; inline $defs) ─────────────


class ConclusionLine(BaseModel):
    """One conclusion in the digest — a dated, sha-cited, human-authored finding.

    The lead of the point query (newest current conclusion first). ``finding`` is
    opaque caller prose: stored, rendered VERBATIM, never parsed. ``cited_shas``
    echoes the sha prefixes the record rests on (the evidence-bound rule);
    ``status`` is the reduced conclusion status.
    """

    model_config = ConfigDict(extra="forbid", title="evidence conclusion line")

    conclusion_id: str = Field(
        description="The conclusion's caller-authored slug (opaque path segment)."
    )
    ts: str = Field(description="The finding's date (the record's ts) — every line dated.")
    tags: list[str] = Field(
        default_factory=list,
        description="The conclusion's own tags (retro-indexing key); opaque caller data.",
    )
    cited_shas: list[str] = Field(
        default_factory=list,
        description="Evidence sha prefixes the conclusion cites (the evidence-bound rule); echoed, never parsed.",
    )
    status: ConclusionStatus = Field(
        description="current | superseded | revoked | absent — the reduced conclusion status.",
    )
    finding: str = Field(
        description="The human's free-text finding — opaque, rendered verbatim, never interpreted.",
    )


class ActivityLine(BaseModel):
    """Per-tag prior-work counts — identity + counting, never a judgment (E-render).

    Renders 'PRIOR WORK · 3 campaigns, 14 runs, 2 lineages · newest 2025-11-02 ·
    9 looks on <tag>'. Every field is a COUNT or a DATE read from the records'
    own fields — no ranking, no urgency.
    """

    model_config = ConfigDict(extra="forbid", title="evidence per-tag activity line")

    tag: str = Field(
        description="The scope-tag slug this activity is counted under (opaque caller data)."
    )
    campaigns: int = Field(default=0, description="Count of campaigns matched under this tag.")
    runs: int = Field(default=0, description="Count of runs matched under this tag.")
    lineages: int = Field(
        default=0, description="Count of distinct command-identity lineages matched."
    )
    looks: int = Field(
        default=0, description="Count of prior looks recorded on this tag's look ledger."
    )
    newest: str | None = Field(
        default=None,
        description="ISO-8601 ts of the newest matched record under this tag (null when none).",
    )


class EnvelopeLine(BaseModel):
    """One lineage's determinism envelope, quoting the fingerprint ledger VERBATIM.

    Renders 'ENVELOPE · lineage 7be4… · ±2.1% rel (n=4: 3 full + 1 partial,
    scales: main, clusters: hoffman2)'. ``envelope`` is the ledger's own rendered
    reduction (never recomputed or reinterpreted here); the evidence-label block
    ``{n, n_full, n_partial, scales, clusters}`` is quoted verbatim from the
    fingerprint ledger (``docs/design/determinism-fingerprint.md``).
    """

    model_config = ConfigDict(extra="forbid", title="evidence envelope line")

    lineage: str = Field(
        description="The command-identity lineage key (cmd_sha) this envelope is for."
    )
    envelope: str = Field(
        description="The ledger's own rendered envelope (e.g. '±2.1% rel'); quoted, never recomputed.",
    )
    n: int = Field(
        default=0, description="Total sample count (full + partial) — the ledger's evidence label."
    )
    n_full: int = Field(
        default=0, description="Full-scale (non-partial) sample count — verbatim from the ledger."
    )
    n_partial: int = Field(
        default=0, description="Partial-scale sample count — verbatim from the ledger."
    )
    scales: list[str] = Field(
        default_factory=list,
        description="Scale labels present in the ledger's evidence block (opaque labels, verbatim).",
    )
    clusters: list[str] = Field(
        default_factory=list,
        description="Cluster labels present in the ledger's evidence block (opaque labels, verbatim).",
    )


class CitationStatusLine(BaseModel):
    """Per-citation re-resolution disclosure at READ (E-shape: disclose, never refuse).

    Evidence legitimately moves after a conclusion is recorded (archived to S3, a
    store re-exported, a repo wiped). The read side re-resolves each citation and
    reports ``verified`` / unresolvable per line — the conclusion stays a truthful
    dated record; the drift is disclosed, the reader decides. (The APPEND gate is
    the deliberate exception where an unresolvable citation REFUSES — that never
    runs here.)
    """

    model_config = ConfigDict(extra="forbid", title="evidence citation-status line")

    conclusion_id: str = Field(description="The conclusion this citation belongs to (opaque slug).")
    kind: CitationKind = Field(
        description="The citation's mechanism kind (closed set) — its one resolver."
    )
    ref: str = Field(
        description="The opaque citation ref (run_id / cmd_sha key / dossier path); echoed, never parsed."
    )
    sha: str = Field(description="The full sha the conclusion recorded for this citation.")
    verified: bool = Field(
        description="True = re-resolved and the sha still matches here; False = unresolvable/mismatched at read time.",
    )


class UnconcludedItem(BaseModel):
    """One terminal campaign with NO conclusion naming it (E-render; period only).

    Pure IDENTITY matching (a terminal campaign whose id appears in no
    conclusion's ``concludes`` set), never text matching. Dated by its completion
    ts so the item AGES honestly — the standing invitation to close the loop. It
    is a LIST entry, never a verdict: a missing conclusion blocks nothing (E3).
    """

    model_config = ConfigDict(extra="forbid", title="evidence unconcluded campaign item")

    scope_kind: str = Field(
        description="The subject kind (campaign / run / scope) with no conclusion naming it."
    )
    scope_id: str = Field(
        description="The subject id (opaque; the identity the unconcluded predicate matched)."
    )
    completed_at: str = Field(
        description="The subject's completion-brief ts — the instant the item ages from."
    )


class SkippedNamespace(BaseModel):
    """One namespace skipped during fleet collection, with the reason (fail-open).

    A wiped repo, an unreadable/torn ``repo.json``, or an absent store must never
    crash the read — it is skipped silently and counted here (the attention-queue
    ``SkippedNamespace`` posture, mirrored).
    """

    model_config = ConfigDict(extra="forbid", title="evidence skipped namespace")

    ref: str = Field(description="The repo_hash / namespace id that was skipped.")
    reason: str = Field(description="Why it was skipped (unreadable, torn, absent).")


# ── the result models ───────────────────────────────────────────────────────


class EvidenceBriefResult(BaseModel):
    """Shape of the ``data`` field on an ``evidence-brief`` envelope (E-verbs).

    The point-query projection: conclusions (newest current first), per-tag
    activity, per-lineage envelopes, per-citation read-time status, and the
    fleet skip accounting — plus the ``render`` markdown the agent relays VERBATIM
    (the ``AttentionQueueResult`` posture). ``computed_at`` dates the whole
    projection; ``cache`` discloses whether it was served, recomputed, or bypassed.
    """

    model_config = ConfigDict(extra="forbid", title="evidence-brief output data")

    computed_at: str = Field(
        description="The instant the digest was computed against (ISO-8601 UTC)."
    )
    as_of: str | None = Field(
        default=None,
        description="The as_of cut the collector applied (echoed from the spec); null when unbounded.",
    )
    conclusions: list[ConclusionLine] = Field(
        default_factory=list,
        description="Dated, sha-cited conclusions — newest current first (the point query's lead).",
    )
    activity: list[ActivityLine] = Field(
        default_factory=list,
        description="Per-tag prior-work counts (campaigns / runs / lineages / looks / newest).",
    )
    envelopes: list[EnvelopeLine] = Field(
        default_factory=list,
        description="Per-lineage determinism envelopes, quoting the fingerprint ledger's evidence block verbatim.",
    )
    citations_status: list[CitationStatusLine] = Field(
        default_factory=list,
        description="Per-citation read-time re-resolution disclosure (verified / unresolvable).",
    )
    skipped: list[SkippedNamespace] = Field(
        default_factory=list,
        description="Namespaces skipped during fleet collection (fail-open accounting).",
    )
    cache: CacheDisposition = Field(
        description="hit | miss | disabled — the cache disposition for this read."
    )
    render: str = Field(
        description="The deterministic markdown digest — relayed to the human verbatim."
    )


class EvidencePeriodResult(BaseModel):
    """Shape of the ``data`` field on an ``evidence-period`` envelope (E-verbs).

    The window projection: the same conclusions / activity / envelopes /
    citation-status / skip accounting as the brief, PLUS the
    ``unconcluded``-campaigns list that terminates the period render (the standing
    invitation to close the conclusion loop). ``render`` rides for verbatim relay.
    """

    model_config = ConfigDict(extra="forbid", title="evidence-period output data")

    computed_at: str = Field(
        description="The instant the digest was computed against (ISO-8601 UTC)."
    )
    as_of: str | None = Field(
        default=None,
        description="The window's upper cut (until), echoed; null when open-ended.",
    )
    conclusions: list[ConclusionLine] = Field(
        default_factory=list,
        description="Dated, sha-cited conclusions in the window — newest first.",
    )
    activity: list[ActivityLine] = Field(
        default_factory=list,
        description="Per-tag prior-work counts within the window.",
    )
    envelopes: list[EnvelopeLine] = Field(
        default_factory=list,
        description="Per-lineage determinism envelopes recorded within the window (ledger-verbatim).",
    )
    unconcluded: list[UnconcludedItem] = Field(
        default_factory=list,
        description="Terminal campaigns in the window with NO conclusion naming them (the loop-closing list).",
    )
    citations_status: list[CitationStatusLine] = Field(
        default_factory=list,
        description="Per-citation read-time re-resolution disclosure (verified / unresolvable).",
    )
    skipped: list[SkippedNamespace] = Field(
        default_factory=list,
        description="Namespaces skipped during fleet collection (fail-open accounting).",
    )
    cache: CacheDisposition = Field(
        description="hit | miss | disabled — the cache disposition for this read."
    )
    render: str = Field(
        description="The deterministic markdown digest — relayed to the human verbatim."
    )
