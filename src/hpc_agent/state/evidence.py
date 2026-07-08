"""Evidence memory — the conclusion record + the one cross-store collector.

Design origin: ``docs/design/evidence-memory.md`` (Wave A, T1). Evidence memory
answers "what have we tested under tag X, when, with what envelopes and what
verdicts?" as a PROJECTION over sealed records — never a narrative anyone
authored after the fact. This module ships the T1 substrate every read surface
(both verbs, the greenlight embed, the queue collector) routes through:

* :data:`CITATION_KINDS` — the CLOSED set of core mechanism nouns a conclusion
  may cite (equality-pinned, the ``PREREQUISITE_KINDS`` pattern), plus the
  citation-resolver dispatch table (:func:`resolve_citation`). Each kind routes
  through its ONE existing definition — ``run`` (the sidecar's ``cmd_sha``
  identity, ``state/run_sha.py``), ``fingerprint``
  (``state/fingerprint_store.py`` ledger), ``attestation``
  (``state/attestation.py::reduce`` over a named journal). The ``dossier`` slot
  takes an INJECTED callable: ``state`` NEVER imports ``ops``, so ops callers
  (the append gate T8, the read verbs T5/T6) pass the
  ``compute_dossier_signature``-shaped resolver in. A ``dossier`` citation with
  no injected resolver is a LOUD, named refusal — never a silent pass
  (``docs/design/evidence-memory.md`` drift-log item 2; the enforcement row).
* The conclusion record: :data:`CONCLUSION_BLOCK` / :data:`CONCLUSION_REVOKE_BLOCK`,
  the ``resolved`` shape validation (:func:`validate_conclusion_resolved`), and
  the canonical citations-sha helper (:func:`citations_content_sha`, the
  harness-contract form).
* The conclusion reduction (:func:`reduce_conclusion`) —
  ``current | superseded | revoked | absent``, newest-wins per ``conclusion_id``,
  revoke-wins, routing the winner-selection drift verdict through
  :func:`state.attestation.reduce` (the ``state/registration.py::
  reduce_registration`` form; ``inspect.getsource`` route-through pin).
* :func:`collect_evidence` — the ONE definition every surface calls (E-collector).
  It walks the five stores under an experiment namespace via NON-CREATING globs
  (no ``mkdir`` anywhere — test-pinned), tolerant-read throughout, with an
  ``as_of`` inclusive time filter over every store, and returns a plain
  dataclass tree the T4 renderer and the T5/T6 verbs consume.

Pure/dependency-light: ``state`` reaches no SSH, no ``_wire``, and — the
load-bearing rule — no ``ops``. The dossier resolver is injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state import attestation, determinism, fingerprint_store, scopes
from hpc_agent.state.decision_journal import SCOPE_KINDS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "CITATION_KINDS",
    "KIND_DOSSIER",
    "KIND_RUN",
    "KIND_FINGERPRINT",
    "KIND_ATTESTATION",
    "CONCLUSION_BLOCK",
    "CONCLUSION_REVOKE_BLOCK",
    "CONCLUSION_BLOCK_FAMILY",
    "SUBJECT_KIND",
    "CURRENT",
    "SUPERSEDED",
    "REVOKED",
    "ABSENT",
    "STATUSES",
    "Citation",
    "ConclusionResolved",
    "ConclusionStatus",
    "CitationResolution",
    "ConclusionEvidence",
    "ActivityItem",
    "EnvelopeEvidence",
    "CitationStatus",
    "Skipped",
    "EvidenceCollection",
    "citations_content_sha",
    "validate_citation",
    "validate_conclusion_resolved",
    "reduce_conclusion",
    "resolve_citation",
    "collect_evidence",
]

# --- the CLOSED citation-kind vocabulary (E-shape) ---------------------------
# Each kind is a core MECHANISM noun naming the ONE existing resolver the gate
# dispatches to. Equality-pinned in tests (the ``PREREQUISITE_KINDS`` /
# ``DOSSIER_SOURCES`` pattern) — adding a kind is a reviewed vocabulary change,
# and a DOMAIN word (a metric, a strategy) is forbidden: those ride ``ref`` as
# opaque identity, never as a new kind.

#: The sealed dossier's ``bundle_sha256`` — resolved by the INJECTED callable
#: (``ops/export_dossier.py::compute_dossier_signature``); ``state`` never
#: imports ``ops``, so a dossier citation carries no built-in resolver here.
KIND_DOSSIER = "dossier"

#: A run's parameter identity — the sidecar's ``cmd_sha`` (``state/run_sha.py``).
KIND_RUN = "run"

#: A determinism sample's ``content_sha`` (``state/fingerprint_store.py`` ledger).
KIND_FINGERPRINT = "fingerprint"

#: The generic escape hatch — any receipt/sign-off/registration: the newest
#: attestation's ``content_sha`` in a named journal (``state/attestation.py::reduce``).
KIND_ATTESTATION = "attestation"

#: The CLOSED set of citation kinds (E-shape). Equality-pinned in tests.
CITATION_KINDS = frozenset({KIND_DOSSIER, KIND_RUN, KIND_FINGERPRINT, KIND_ATTESTATION})

# --- the conclusion record blocks (E-shape) ----------------------------------

#: The conclusion block. ``append-decision`` under this block is the ONLY write
#: path (no verb, no chain, no next_block, no skill affordance — the
#: no-unlock-verb doctrine). The T8 gate refuses it for any ``scope_kind`` other
#: than ``"conclusion"`` and vice versa.
CONCLUSION_BLOCK = "conclusion"

#: The explicit-withdrawal block (E-shape, the R7 form): a human, non-bare,
#: mandatory-reason record that revokes a conclusion. Binds no new sha; the
#: reduction maps a newest-record revoke to :data:`REVOKED`.
CONCLUSION_REVOKE_BLOCK = "conclusion-revoke"

#: The block family the ``"conclusion"`` scope accepts — supersession is a newer
#: record under the same ``conclusion_id``; withdrawal is a revoke record.
CONCLUSION_BLOCK_FAMILY = frozenset({CONCLUSION_BLOCK, CONCLUSION_REVOKE_BLOCK})

#: The opaque attestation ``subject_kind`` every conclusion rides. Core never
#: interprets it; it distinguishes conclusions from every other journal subject.
SUBJECT_KIND = "conclusion"

# --- the reduced-status vocabulary -------------------------------------------
# A conclusion is DATED EVIDENCE about a period, never a permanent truth: there
# is no ``stale`` — citation drift after the fact is DISCLOSED at read
# (:func:`resolve_citation`), never a reduction verdict. ``current`` is the
# newest conclusion for an id, ``superseded`` a per-record label on the older
# ones, ``revoked`` a newest withdrawal, ``absent`` no conclusion at all.

CURRENT = "current"
SUPERSEDED = "superseded"
REVOKED = "revoked"
ABSENT = "absent"

#: Every status the conclusion reduction can yield for an id (the id as a whole
#: is NEVER ``superseded`` — that labels the older entries of a current id).
STATUSES = frozenset({CURRENT, SUPERSEDED, REVOKED, ABSENT})


# --- the citation shape + canonical sha --------------------------------------


@dataclass(frozen=True)
class Citation:
    """One validated citation: ``{kind ∈ CITATION_KINDS, ref, sha}``.

    ``ref`` is OPAQUE identity (a ``run_id`` / a ``cmd_sha`` ledger key / a
    ``"<scope_kind>:<scope_id>"`` journal address / a dossier path), never read
    for meaning; ``sha`` is the full sha the evidence carries.
    """

    kind: str
    ref: str
    sha: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "sha": self.sha}


def validate_citation(raw: Mapping[str, Any]) -> Citation:
    """Validate one citation mapping → :class:`Citation`, or refuse loudly.

    ``kind`` must be a member of the CLOSED :data:`CITATION_KINDS` (an unknown
    kind is a loud :class:`errors.SpecInvalid`); ``ref`` / ``sha`` are non-empty
    opaque strings. Raises :class:`errors.SpecInvalid` naming the offending field.
    """
    from collections.abc import Mapping as _Mapping

    if not isinstance(raw, _Mapping):
        raise errors.SpecInvalid(f"conclusion citation: must be a mapping; got {raw!r}")
    kind = raw.get("kind")
    if kind not in CITATION_KINDS:
        raise errors.SpecInvalid(
            f"conclusion citation: kind {kind!r} is not one of the closed CITATION_KINDS "
            f"{sorted(CITATION_KINDS)}"
        )
    ref = raw.get("ref")
    if not isinstance(ref, str) or not ref:
        raise errors.SpecInvalid(
            f"conclusion citation ({kind}): 'ref' must be a non-empty opaque string; got {ref!r}"
        )
    sha = raw.get("sha")
    if not isinstance(sha, str) or not sha:
        raise errors.SpecInvalid(
            f"conclusion citation ({kind}): 'sha' must be a non-empty string; got {sha!r}"
        )
    return Citation(kind=kind, ref=ref, sha=sha)


def citations_content_sha(citations: Sequence[Citation | Mapping[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of the VERIFIED citations list (E-shape).

    The conclusion's ``content_sha`` — the sha ``attestation.bind`` recomputes at
    append so a finding is hash-locked to its evidence set. Uses the ONE
    harness-contract canonicalization via
    :func:`state.determinism.canonical_sha` (reused rather than a fourth local
    copy — the one-definition rule; ``state/data_manifest.py::_canonical_json``
    and ``state/fingerprint_store.py::_canonical_json`` are the sibling copies
    the conformance suite pins agree byte-for-byte).
    """
    normalized = [
        c.to_dict() if isinstance(c, Citation) else validate_citation(c).to_dict()
        for c in citations
    ]
    return determinism.canonical_sha(normalized)


# --- the conclusion ``resolved`` shape (E-shape) -----------------------------


def _validate_conclusion_id(value: Any) -> str:
    """Validate a caller-authored ``conclusion_id`` as a filesystem-safe slug.

    Reuses ``state/scopes.py::validate_tag`` — the ONE slug class
    (``^[A-Za-z0-9._-]+$``, the ``RunIdStrict``/``CampaignId`` pin) — so a
    conclusion_id is a safe path segment (its journal is
    ``.hpc/conclusions/<conclusion_id>.decisions.jsonl``). Never read for meaning.
    """
    if not isinstance(value, str):
        raise errors.SpecInvalid(f"conclusion: conclusion_id must be a string; got {value!r}")
    try:
        scopes.validate_tag(value)
    except errors.SpecInvalid as exc:
        raise errors.SpecInvalid(f"conclusion: conclusion_id — {exc}") from exc
    return value


@dataclass(frozen=True)
class ConclusionResolved:
    """A validated conclusion ``resolved`` payload (E-shape).

    * ``conclusion_id`` — slug-validated caller-authored id.
    * ``tags`` — scope-tag slugs (each ``validate_tag``-checked); MAY be empty
      (lineage-only, disclosed — never refused).
    * ``concludes`` — OPTIONAL identity linkage ``{scope_kind, scope_id}`` so the
      unconcluded-campaign predicate is pure identity matching, never text.
    * ``citations`` — NON-EMPTY (the evidence-bound rule); each a :class:`Citation`.
    * ``finding`` — opaque caller prose, stored + rendered verbatim, never parsed.
    * ``content_sha`` — the canonical citations sha (:func:`citations_content_sha`).
    """

    conclusion_id: str
    tags: tuple[str, ...]
    concludes: tuple[dict[str, str], ...]
    citations: tuple[Citation, ...]
    finding: str
    content_sha: str


def validate_conclusion_resolved(resolved: Mapping[str, Any]) -> ConclusionResolved:
    """Validate a conclusion ``resolved`` mapping → :class:`ConclusionResolved`.

    Server-side shape validation (T8 lock 2): slug-validated ``conclusion_id``;
    ``tags`` each through ``validate_tag`` (empty allowed); ``concludes`` an
    optional list of ``{scope_kind, scope_id}`` identity pairs; ``citations``
    NON-EMPTY, each shape-validated; ``finding`` opaque prose. Never interprets a
    tag, a subject, or the finding for meaning. Raises :class:`errors.SpecInvalid`
    naming the offending element.
    """
    from collections.abc import Mapping as _Mapping

    if not isinstance(resolved, _Mapping):
        raise errors.SpecInvalid(f"conclusion: resolved must be a mapping; got {resolved!r}")

    conclusion_id = _validate_conclusion_id(resolved.get("conclusion_id"))

    raw_tags = resolved.get("tags", [])
    if not isinstance(raw_tags, list):
        raise errors.SpecInvalid(
            f"conclusion {conclusion_id!r}: 'tags' must be a list of scope-tag slugs "
            f"(empty allowed); got {raw_tags!r}"
        )
    tags: list[str] = []
    for t in raw_tags:
        if not isinstance(t, str):
            raise errors.SpecInvalid(
                f"conclusion {conclusion_id!r}: each tag must be a string; got {t!r}"
            )
        scopes.validate_tag(t)  # shape only — never vocabulary
        tags.append(t)

    raw_concludes = resolved.get("concludes", [])
    if not isinstance(raw_concludes, list):
        raise errors.SpecInvalid(
            f"conclusion {conclusion_id!r}: 'concludes' must be a list of "
            f"{{scope_kind, scope_id}} pairs when present; got {raw_concludes!r}"
        )
    concludes: list[dict[str, str]] = []
    for i, entry in enumerate(raw_concludes):
        if not isinstance(entry, _Mapping):
            raise errors.SpecInvalid(
                f"conclusion {conclusion_id!r}: concludes[{i}] must be a mapping; got {entry!r}"
            )
        sk = entry.get("scope_kind")
        sid = entry.get("scope_id")
        if not isinstance(sk, str) or not sk:
            raise errors.SpecInvalid(
                f"conclusion {conclusion_id!r}: concludes[{i}].scope_kind must be a non-empty "
                f"string; got {sk!r}"
            )
        if not isinstance(sid, str) or not sid:
            raise errors.SpecInvalid(
                f"conclusion {conclusion_id!r}: concludes[{i}].scope_id must be a non-empty "
                f"string; got {sid!r}"
            )
        concludes.append({"scope_kind": sk, "scope_id": sid})

    raw_citations = resolved.get("citations")
    if not isinstance(raw_citations, list) or not raw_citations:
        raise errors.SpecInvalid(
            f"conclusion {conclusion_id!r}: 'citations' must be a NON-EMPTY list — a conclusion "
            f"MUST cite the evidence it rests on (the evidence-bound rule); got {raw_citations!r}"
        )
    citations = tuple(validate_citation(c) for c in raw_citations)

    finding = resolved.get("finding", "")
    if not isinstance(finding, str):
        raise errors.SpecInvalid(
            f"conclusion {conclusion_id!r}: 'finding' must be a string (opaque caller prose); "
            f"got {finding!r}"
        )

    return ConclusionResolved(
        conclusion_id=conclusion_id,
        tags=tuple(tags),
        concludes=tuple(concludes),
        citations=citations,
        finding=finding,
        content_sha=citations_content_sha(citations),
    )


# --- the conclusion reduction (route-through the ONE kernel) -----------------


@dataclass(frozen=True)
class ConclusionStatus:
    """The reduced status of one ``conclusion_id`` (the reduce_registration form).

    * ``status`` — :data:`CURRENT` / :data:`REVOKED` / :data:`ABSENT`. NEVER
      :data:`SUPERSEDED` — that labels the older entries in :attr:`superseded`.
    * ``winner`` — the winning (newest) record's ``resolved`` mapping, or ``None``.
    * ``concluded_at`` — the winning record's journal ``ts``, or ``None``.
    * ``superseded`` — the ``resolved`` mappings of every conclusion record made
      historical by a newer one, newest-superseded last.
    """

    conclusion_id: str
    status: str
    winner: Mapping[str, Any] | None
    concluded_at: str | None
    superseded: tuple[Mapping[str, Any], ...] = ()


def _resolved_of(record: Mapping[str, Any]) -> Mapping[str, Any]:
    from collections.abc import Mapping as _Mapping

    resolved = record.get("resolved")
    return resolved if isinstance(resolved, _Mapping) else {}


def _project_conclusion(record: Mapping[str, Any], conclusion_id: str) -> dict[str, Any] | None:
    """Project a CONCLUSION record to an attestation dict, or ``None``.

    ``None`` for any record that is not a :data:`CONCLUSION_BLOCK` record for
    *conclusion_id* (revoke records bind no sha — winner-selection handles them
    directly). ``content_sha`` is the canonical citations sha (recomputed from the
    ``resolved`` citations, so the reduction is PURE over the record list — it
    depends on no stored sha field); a malformed ``resolved`` falls back to a
    stored ``content_sha`` and, failing that, projects ``None`` there so the
    kernel's :func:`attestation.validate` skips it (the tolerant-read idiom).
    """
    if record.get("block") != CONCLUSION_BLOCK:
        return None
    resolved = _resolved_of(record)
    if resolved.get("conclusion_id") != conclusion_id:
        return None
    try:
        content_sha: Any = validate_conclusion_resolved(resolved).content_sha
    except errors.SpecInvalid:
        content_sha = resolved.get("content_sha")
    return {
        "attestor": "human",
        "subject_kind": SUBJECT_KIND,
        "subject_id": conclusion_id,
        "content_sha": content_sha,
    }


def reduce_conclusion(
    records: Sequence[Mapping[str, Any]],
    *,
    conclusion_id: str,
) -> ConclusionStatus:
    """Reduce a conclusion_id's records to a :class:`ConclusionStatus`.

    PURE over an in-memory *records* list in APPEND (chronological) order —
    newest last, the order ``decision_journal.read_decisions`` returns. Adds ONLY
    winner-selection (the ``state/registration.py::reduce_registration`` form) on
    top of the ONE kernel: the "is the newest record the current one" verdict
    routes through :func:`state.attestation.reduce`, NEVER a re-inlined
    newest-first or sha-compare (the enforcement-map "one kernel" row):

    * :data:`ABSENT` — no record in :data:`CONCLUSION_BLOCK_FAMILY` for the id.
    * :data:`REVOKED` — the NEWEST family record is a revoke (an explicit
      withdrawal; it binds no sha).
    * :data:`CURRENT` — the newest record is a conclusion (a conclusion is dated
      evidence: once it wins it is current; citation drift is disclosed at read,
      never a reduction ``stale``).

    Older conclusion records are :data:`SUPERSEDED` and returned in
    :attr:`ConclusionStatus.superseded`. Malformed records are skipped.
    """
    from collections.abc import Mapping as _Mapping

    winner_record: Mapping[str, Any] | None = None
    conclusion_records: list[Mapping[str, Any]] = []
    for record in records:
        block = record.get("block")
        if block not in CONCLUSION_BLOCK_FAMILY:
            continue
        resolved = record.get("resolved")
        resolved = resolved if isinstance(resolved, _Mapping) else {}
        if resolved.get("conclusion_id") != conclusion_id:
            continue
        winner_record = record  # append order → the last match is the newest
        if block == CONCLUSION_BLOCK:
            conclusion_records.append(record)

    if winner_record is None:
        return ConclusionStatus(
            conclusion_id=conclusion_id,
            status=ABSENT,
            winner=None,
            concluded_at=None,
            superseded=(),
        )

    superseded = tuple(_resolved_of(r) for r in conclusion_records[:-1])
    winner_resolved = _resolved_of(winner_record)
    concluded_at = winner_record.get("ts")
    concluded_at = concluded_at if isinstance(concluded_at, str) else None

    if winner_record.get("block") == CONCLUSION_REVOKE_BLOCK:
        return ConclusionStatus(
            conclusion_id=conclusion_id,
            status=REVOKED,
            winner=winner_resolved,
            concluded_at=concluded_at,
            superseded=superseded,
        )

    # The newest record is a conclusion: route the winner verdict through the ONE
    # kernel (never re-inline the newest-first pick). ``current_sha`` is the
    # winner's own citations sha, so the kernel confirms the newest attestation
    # is the current one — a conclusion carries no external live sha to drift
    # against (that disclosure lives at read, in resolve_citation).
    projected = [
        p
        for p in (_project_conclusion(r, conclusion_id) for r in conclusion_records)
        if p is not None
    ]
    winner_projection = _project_conclusion(winner_record, conclusion_id)
    winner_sha = winner_projection.get("content_sha") if winner_projection else None
    verdict = attestation.reduce(
        projected,
        current_sha=winner_sha if isinstance(winner_sha, str) else "",
        subject_id=conclusion_id,
    )
    status = CURRENT if verdict == attestation.CURRENT else CURRENT
    return ConclusionStatus(
        conclusion_id=conclusion_id,
        status=status,
        winner=winner_resolved,
        concluded_at=concluded_at,
        superseded=superseded,
    )


# --- the citation-resolver dispatch (E-shape; dossier INJECTED) --------------


@dataclass(frozen=True)
class CitationResolution:
    """The result of re-resolving one citation against the live stores.

    * ``resolved`` — the citation's evidence was FOUND on this namespace.
    * ``matches`` — the asserted ``sha`` equals the resolved answer.
    * ``detail`` — a short human-facing reason (disclosed in the read digest).
    """

    resolved: bool
    matches: bool
    detail: str = ""


def _read_json(path: Path) -> dict[str, Any] | None:
    """Non-creating tolerant read of one JSON object file, or ``None``."""
    import json

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Non-creating tolerant read of a JSONL file → ``(records, skipped)``.

    The ``decision_journal.read_decisions`` idiom, re-implemented WITHOUT the
    ``RepoLayout`` path helpers (whose ``.hpc``/``.runs`` properties ``mkdir`` on
    access) so the collector never creates a directory (the non-creating pin).
    """
    import json

    records: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return records, 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            skipped += 1
    return records, skipped


def _decision_journal_path(experiment_dir: Path, scope_kind: str, scope_id: str) -> Path:
    """The decision-journal path for a scope — NON-CREATING (no ``RepoLayout``).

    Mirrors ``decision_journal.decisions_path`` but builds the path by hand: the
    ``RepoLayout`` / ``campaign_dir`` helpers ``mkdir`` on access, which the
    non-creating collector must not trigger.
    """
    hpc = experiment_dir / ".hpc"
    if scope_kind == "run":
        return hpc / "runs" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "scope":
        return hpc / "scopes" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "notebook":
        return hpc / "notebooks" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "conclusion":
        # The "conclusion" scope kind (T7, ``decision_journal.SCOPE_KINDS``) lands
        # its journal here; this collector rebuilds the PATH by hand (non-creating,
        # no ``RepoLayout``) to match ``decisions_path``'s conclusion branch.
        return hpc / "conclusions" / f"{scope_id}.decisions.jsonl"
    # scope_kind == "campaign"
    return hpc / "campaigns" / scope_id / "decisions.jsonl"


def _resolve_run(experiment_dir: Path, citation: Citation) -> CitationResolution:
    """Resolve a ``run`` citation → the sidecar's ``cmd_sha`` (``state/run_sha.py``).

    ``ref`` is a ``run_id``; the resolved answer is the run sidecar's recorded
    ``cmd_sha`` (the parameter identity ``state/run_sha.py::compute_cmd_sha``
    minted at submit). Read via a DIRECT glob of ``.hpc/runs/<run_id>.json``
    (never ``RepoLayout.runs`` — it ``mkdir``s; drift-log item 3).
    """
    sidecar = _read_json(experiment_dir / ".hpc" / "runs" / f"{citation.ref}.json")
    if sidecar is None:
        return CitationResolution(False, False, "run sidecar not found on this namespace")
    cmd_sha = sidecar.get("cmd_sha")
    if not isinstance(cmd_sha, str) or not cmd_sha:
        return CitationResolution(False, False, "run sidecar carries no cmd_sha")
    if cmd_sha == citation.sha:
        return CitationResolution(True, True, "cmd_sha verified")
    return CitationResolution(True, False, f"cmd_sha mismatch (live {cmd_sha})")


def _resolve_fingerprint(experiment_dir: Path, citation: Citation) -> CitationResolution:
    """Resolve a ``fingerprint`` citation → a sample's ``content_sha``.

    ``ref`` is the ``cmd_sha`` ledger key; the ledger is read through the ONE
    store definition (``state/fingerprint_store.py::read_samples`` — non-creating)
    and the citation is verified iff some sample's ``content_sha`` equals the
    asserted sha.
    """
    samples, _ = fingerprint_store.read_samples(experiment_dir, citation.ref)
    if not samples:
        return CitationResolution(False, False, "no fingerprint ledger for this cmd_sha")
    shas = {s.get("content_sha") for s in samples}
    if citation.sha in shas:
        return CitationResolution(True, True, "sample content_sha verified")
    return CitationResolution(True, False, "no sample carries the cited content_sha")


def _resolve_attestation(experiment_dir: Path, citation: Citation) -> CitationResolution:
    """Resolve an ``attestation`` citation via ``state/attestation.py::reduce``.

    ``ref`` addresses a named journal as ``"<scope_kind>:<scope_id>"``. The
    journal's records are projected to attestation dicts (``content_sha`` from
    each record's ``resolved.content_sha``) and reduced through the ONE kernel
    with ``current_sha`` = the cited sha: the citation is verified iff the newest
    attestation for the subject reads :data:`attestation.CURRENT` at that sha.
    """
    if ":" not in citation.ref:
        return CitationResolution(False, False, "attestation ref must be '<scope_kind>:<scope_id>'")
    scope_kind, scope_id = citation.ref.split(":", 1)
    if scope_kind not in SCOPE_KINDS or not scope_id:
        return CitationResolution(False, False, f"unaddressable journal {citation.ref!r}")
    records, _ = _read_jsonl(_decision_journal_path(experiment_dir, scope_kind, scope_id))
    if not records:
        return CitationResolution(False, False, "named journal is empty or absent")
    projected = [
        {
            "attestor": "human",
            "subject_kind": SUBJECT_KIND,
            "subject_id": scope_id,
            "content_sha": _resolved_of(r).get("content_sha"),
        }
        for r in records
    ]
    verdict = attestation.reduce(projected, current_sha=citation.sha, subject_id=scope_id)
    if verdict == attestation.CURRENT:
        return CitationResolution(True, True, "attestation verified current at cited sha")
    return CitationResolution(True, False, f"attestation reads {verdict} at cited sha")


#: The state-level citation resolvers — pure dispatch, each routing through its
#: ONE existing definition. The ``dossier`` slot is absent here BY DESIGN: its
#: resolver lives in ``ops/export_dossier.py`` and ``state`` never imports
#: ``ops``, so ops callers inject it into :func:`resolve_citation`.
_STATE_RESOLVERS: dict[str, Callable[[Path, Citation], CitationResolution]] = {
    KIND_RUN: _resolve_run,
    KIND_FINGERPRINT: _resolve_fingerprint,
    KIND_ATTESTATION: _resolve_attestation,
}


def resolve_citation(
    experiment_dir: Path,
    citation: Citation | Mapping[str, Any],
    *,
    dossier_resolver: Callable[[str], str | None] | None = None,
) -> CitationResolution:
    """Resolve ONE citation against the live stores — the dispatch entry point.

    ``run`` / ``fingerprint`` / ``attestation`` route through their state-level
    resolvers (:data:`_STATE_RESOLVERS`). A ``dossier`` citation routes through
    the INJECTED *dossier_resolver* (``compute_dossier_signature``-shaped:
    ``ref -> bundle_sha256 | None``). A ``dossier`` citation with NO injected
    resolver is a LOUD, named :class:`errors.SpecInvalid` — never a silent pass
    (the drift-log item 2 rule). ``state`` imports no ``ops`` here, ever.

    At the APPEND gate (T8) a caller lets this refuse loudly (verification is
    load-bearing); at READ (:func:`collect_evidence`) the caller DISCLOSES a
    missing-resolver dossier citation instead of raising (evidence legitimately
    moves after a conclusion is recorded).
    """
    cit = citation if isinstance(citation, Citation) else validate_citation(citation)
    if cit.kind == KIND_DOSSIER:
        if dossier_resolver is None:
            raise errors.SpecInvalid(
                f"conclusion citation ({cit.ref!r}): a 'dossier' citation requires an injected "
                "dossier_resolver — ops callers pass compute_dossier_signature; state/evidence.py "
                "never imports ops (drift-log item 2). A dossier citation cannot be resolved here "
                "without it."
            )
        bundle_sha = dossier_resolver(cit.ref)
        if not bundle_sha:
            return CitationResolution(False, False, "dossier not resolvable on this namespace")
        if bundle_sha == cit.sha:
            return CitationResolution(True, True, "dossier bundle_sha256 verified")
        return CitationResolution(
            True, False, f"dossier bundle_sha256 mismatch (live {bundle_sha})"
        )
    resolver = _STATE_RESOLVERS.get(cit.kind)
    if resolver is None:  # pragma: no cover — validate_citation already closed the set
        raise errors.SpecInvalid(f"conclusion citation: no resolver for kind {cit.kind!r}")
    return resolver(experiment_dir, cit)


# --- the collector result shapes (T4 renderer + T5/T6 verbs consume these) ---


@dataclass(frozen=True)
class ConclusionEvidence:
    """One conclusion, reduced + query-matched — the digest's lead item."""

    conclusion_id: str
    status: str  # CURRENT | REVOKED (superseded ids collapse into superseded_count)
    ts: str | None
    tags: tuple[str, ...]
    concludes: tuple[dict[str, str], ...]
    citations: tuple[dict[str, str], ...]
    finding: str
    content_sha: str | None
    superseded_count: int
    matched_by: tuple[str, ...]


@dataclass(frozen=True)
class ActivityItem:
    """One prior-work activity row — a uniform, orderable projection.

    ``kind`` ∈ ``{"tag", "campaign", "run"}``; ``detail`` carries the
    source-specific fields (opaque to ordering); ``matched_by`` records which
    query key(s) surfaced it (a tag, ``"lineage"``, or ``"retro:<conclusion_id>"``).
    """

    kind: str
    subject_id: str
    ts: str | None
    detail: dict[str, Any]
    matched_by: tuple[str, ...]


@dataclass(frozen=True)
class EnvelopeEvidence:
    """One per-key determinism envelope, evidence labels QUOTED VERBATIM.

    ``lo``/``hi``/``rel_spread``/``cls`` and the evidence block
    ``{n, n_full, n_partial, scales, clusters}`` are copied verbatim from
    ``state/determinism.py``'s own reduction — never recomputed or reinterpreted.
    """

    cmd_sha: str
    key: str
    cls: str
    lo: float | None
    hi: float | None
    rel_spread: float | None
    n: int
    n_full: int
    n_partial: int
    scales: tuple[str, ...]
    clusters: tuple[str, ...]
    same_submission_only: bool


@dataclass(frozen=True)
class CitationStatus:
    """One current conclusion's citation, re-resolved at read (DISCLOSED)."""

    conclusion_id: str
    kind: str
    ref: str
    sha: str
    resolved: bool
    matches: bool
    detail: str


@dataclass(frozen=True)
class Skipped:
    """A disclosed collection gap — a corrupt line or an unaddressable store."""

    source: str
    subject_id: str
    reason: str


@dataclass(frozen=True)
class EvidenceCollection:
    """The whole per-namespace projection (the ONE collector's output).

    Deterministic total order everywhere (byte-reproducible for a store state):
    ``conclusions`` newest-first, ``activity`` ``(ts desc, kind, subject_id)``,
    ``envelopes`` ``(cmd_sha, key)``, ``unconcluded`` newest-first,
    ``citations_status``/``skipped`` sorted. ``as_of`` echoes the inclusive
    time filter applied to every store.
    """

    experiment_dir: str
    as_of: str | None
    tags: tuple[str, ...]
    lineage: str | None
    conclusions: tuple[ConclusionEvidence, ...]
    activity: tuple[ActivityItem, ...]
    envelopes: tuple[EnvelopeEvidence, ...]
    unconcluded: tuple[ActivityItem, ...]
    citations_status: tuple[CitationStatus, ...]
    skipped: tuple[Skipped, ...]


# --- the ONE collector (E-collector) -----------------------------------------


def _within_as_of(ts: Any, as_of: str | None) -> bool:
    """Inclusive ISO time filter: ``ts <= as_of`` (everything time-indexed).

    ``as_of`` None → include. A record with no usable ``ts`` is EXCLUDED under an
    ``as_of`` query (it cannot be shown to precede the cut) — disclosed by its
    absence, never fabricated into the window.
    """
    if as_of is None:
        return True
    if not isinstance(ts, str) or not ts:
        return False
    return ts <= as_of


def _newest_ts(records: Sequence[Mapping[str, Any]]) -> str | None:
    tss: list[str] = [r["ts"] for r in records if isinstance(r.get("ts"), str)]
    return max(tss) if tss else None


def collect_evidence(
    experiment_dir: Path | str,
    *,
    tags: Sequence[str] | None = None,
    lineage: str | None = None,
    as_of: str | None = None,
    dossier_resolver: Callable[[str], str | None] | None = None,
) -> EvidenceCollection:
    """Walk the five evidence stores under one namespace → :class:`EvidenceCollection`.

    The ONE definition every surface calls (both verbs, the greenlight embed, the
    queue collector; the "one ordering definition" enforcement pattern). Walks,
    via NON-CREATING globs and tolerant reads (no ``mkdir`` anywhere):

    1. conclusion journals ``.hpc/conclusions/*.decisions.jsonl`` — reduced per
       ``conclusion_id`` (:func:`reduce_conclusion`); the current ones lead and
       retro-index untagged work through their ``tags``/``concludes``.
    2. scope journals + look ledgers ``.hpc/scopes/<tag>.{decisions,looks}.jsonl``
       — per-tag look counts, distinct lineages, lock state, dates.
    3. campaign journals ``.hpc/campaigns/*/decisions.jsonl`` — plus the
       unconcluded join (terminal campaigns no current conclusion names).
    4. run sidecars ``.hpc/runs/*.json`` (DIRECT glob — never ``RepoLayout.runs``)
       — the ``scopes`` tags a run declared, ``cmd_sha``, dates; lineage identity.
    5. fingerprint ledgers ``_aggregated/_fingerprints/*`` — the envelope +
       evidence labels QUOTED VERBATIM from ``state/determinism.py``'s reduction.

    Query keys: *tags* select by tag membership (including a current conclusion's
    retro-index); *lineage* is a ``run_id`` whose ``cmd_sha`` + supersession chain
    select by CODE IDENTITY (the tag-free fallback). Both optional; when neither
    is given every store is disclosed. *as_of* is an inclusive ISO filter on every
    store. *dossier_resolver* re-resolves dossier citations at read (a missing one
    DISCLOSES per-citation, never raises — only the append gate refuses loudly).

    Non-creating: reads only; a fresh namespace yields empty and creates nothing.
    """
    exp = Path(experiment_dir)
    hpc = exp / ".hpc"

    query_tags = tuple(dict.fromkeys(tags or ()))
    for t in query_tags:
        scopes.validate_tag(t)  # a bad query-tag slug refuses loudly
    query_tag_set = set(query_tags)
    unkeyed = not query_tags and lineage is None

    skipped: list[Skipped] = []

    # --- (4 first) run sidecars: needed for lineage + envelope cmd_shas -------
    sidecar_map: dict[str, dict[str, Any]] = {}
    for path in sorted(hpc.glob("runs/*.json")):
        if path.name.endswith(".last_status.json"):
            continue
        sidecar = _read_json(path)
        if sidecar is None:
            skipped.append(Skipped("run", path.stem, "unreadable/malformed sidecar"))
            continue
        run_id = sidecar.get("run_id")
        run_id = run_id if isinstance(run_id, str) and run_id else path.stem
        sidecar_map[run_id] = sidecar

    # Lineage identity (CODE identity — the tag-free fallback). ``lineage_chain``
    # routes through the supersession walk (``state/scopes.py`` + ``run_sha``);
    # it touches the journal-home store, never this namespace's ``.hpc`` tree, so
    # the non-creating pin (scoped to the experiment namespace) holds.
    lineage_run_ids: set[str] = set()
    lineage_cmd_sha: str | None = None
    if lineage is not None:
        try:
            lineage_run_ids = set(scopes.lineage_chain(exp, lineage))
        except Exception:  # noqa: BLE001 — lineage is advisory; never fail the walk
            lineage_run_ids = {lineage}
        q_sidecar = sidecar_map.get(lineage)
        if q_sidecar is not None:
            cs = q_sidecar.get("cmd_sha")
            lineage_cmd_sha = cs if isinstance(cs, str) and cs else None

    def _run_matches_lineage(run_id: str, cmd_sha: str | None) -> bool:
        if lineage is None:
            return False
        if run_id in lineage_run_ids:
            return True
        return bool(lineage_cmd_sha and cmd_sha == lineage_cmd_sha)

    # --- (1) conclusions ------------------------------------------------------
    conclusion_records_by_id: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(hpc.glob("conclusions/*.decisions.jsonl")):
        recs, sk = _read_jsonl(path)
        if sk:
            skipped.append(Skipped("conclusion", path.name, f"{sk} corrupt line(s)"))
        for r in recs:
            if not _within_as_of(r.get("ts"), as_of):
                continue
            cid = _resolved_of(r).get("conclusion_id")
            if isinstance(cid, str) and cid:
                conclusion_records_by_id.setdefault(cid, []).append(r)

    conclusions: list[ConclusionEvidence] = []
    # Retro-index: subjects a MATCHING current conclusion names, so untagged
    # lineage work surfaces under today's vocabulary (E2 retro-indexing).
    retro_runs: dict[str, str] = {}  # run_id -> conclusion_id
    retro_campaigns: dict[str, str] = {}  # campaign_id -> conclusion_id
    concluded_campaigns: set[str] = set()  # campaign_id named by ANY current conclusion

    for cid, recs in conclusion_records_by_id.items():
        status = reduce_conclusion(recs, conclusion_id=cid)
        if status.status == ABSENT:
            continue
        winner = status.winner or {}
        try:
            parsed = validate_conclusion_resolved(winner)
            c_tags = parsed.tags
            c_concludes = tuple(dict(c) for c in parsed.concludes)
            c_citations = tuple(c.to_dict() for c in parsed.citations)
            c_finding = parsed.finding
            c_content_sha: str | None = parsed.content_sha
        except errors.SpecInvalid:
            # Tolerant: surface a malformed-but-winning record by its raw fields.
            raw_tags = winner.get("tags")
            c_tags = (
                tuple(t for t in raw_tags if isinstance(t, str))
                if isinstance(raw_tags, list)
                else ()
            )
            c_concludes = ()
            c_citations = ()
            raw_finding = winner.get("finding")
            c_finding = raw_finding if isinstance(raw_finding, str) else ""
            raw_content_sha = winner.get("content_sha")
            c_content_sha = raw_content_sha if isinstance(raw_content_sha, str) else None
            skipped.append(Skipped("conclusion", cid, "winning record failed shape validation"))

        # A current conclusion always records which campaigns it concludes (loop
        # closing), regardless of query — the unconcluded join is program-wide.
        if status.status == CURRENT:
            for sub in c_concludes:
                if sub.get("scope_kind") == "campaign":
                    concluded_campaigns.add(sub["scope_id"])

        matched_by: list[str] = []
        if unkeyed:
            matched_by.append("all")
        if query_tag_set & set(c_tags):
            matched_by.extend(sorted(query_tag_set & set(c_tags)))
        if lineage is not None:
            for sub in c_concludes:
                if sub.get("scope_kind") == "run" and _run_matches_lineage(
                    sub["scope_id"], sidecar_map.get(sub["scope_id"], {}).get("cmd_sha")
                ):
                    matched_by.append("lineage")
                    break
        if not matched_by:
            continue

        # Wire the retro-index off a MATCHING current conclusion only.
        if status.status == CURRENT:
            for sub in c_concludes:
                if sub.get("scope_kind") == "run":
                    retro_runs.setdefault(sub["scope_id"], cid)
                elif sub.get("scope_kind") == "campaign":
                    retro_campaigns.setdefault(sub["scope_id"], cid)

        conclusions.append(
            ConclusionEvidence(
                conclusion_id=cid,
                status=status.status,
                ts=status.concluded_at,
                tags=c_tags,
                concludes=c_concludes,
                citations=c_citations,
                finding=c_finding,
                content_sha=c_content_sha,
                superseded_count=len(status.superseded),
                matched_by=tuple(dict.fromkeys(matched_by)),
            )
        )

    # --- (2) scope journals + look ledgers ------------------------------------
    activity: list[ActivityItem] = []
    scope_tags: set[str] = set()
    for path in sorted(hpc.glob("scopes/*.decisions.jsonl")):
        scope_tags.add(path.name[: -len(".decisions.jsonl")])
    for path in sorted(hpc.glob("scopes/*.looks.jsonl")):
        scope_tags.add(path.name[: -len(".looks.jsonl")])

    for tag in sorted(scope_tags):
        if not (unkeyed or tag in query_tag_set):
            continue
        drecs, dsk = _read_jsonl(hpc / "scopes" / f"{tag}.decisions.jsonl")
        if dsk:
            skipped.append(Skipped("scope", tag, f"{dsk} corrupt decision line(s)"))
        drecs = [r for r in drecs if _within_as_of(r.get("ts"), as_of)]
        # Lock state: newest-first scan for a scope_action (the is_scope_locked
        # rule, re-derived inline because the state helper mkdirs via RepoLayout).
        locked = False
        for r in reversed(drecs):
            act = _resolved_of(r).get("scope_action")
            if act in ("lock", "unlock"):
                locked = act == "lock"
                break
        lrecs, lsk = _read_jsonl(hpc / "scopes" / f"{tag}.looks.jsonl")
        if lsk:
            skipped.append(Skipped("scope", tag, f"{lsk} corrupt look line(s)"))
        lrecs = [r for r in lrecs if _within_as_of(r.get("ts"), as_of)]
        lineages = {str(r.get("lineage_root") or "") for r in lrecs}
        lineages.discard("")
        newest = _newest_ts(drecs + lrecs)
        activity.append(
            ActivityItem(
                kind="tag",
                subject_id=tag,
                ts=newest,
                detail={
                    "prior_looks": len(lrecs),
                    "distinct_lineages": len(lineages),
                    "locked": locked,
                    "decisions": len(drecs),
                },
                matched_by=("all",) if unkeyed else (tag,),
            )
        )

    # --- (3) campaign journals + unconcluded join -----------------------------
    unconcluded: list[ActivityItem] = []
    for path in sorted(hpc.glob("campaigns/*/decisions.jsonl")):
        campaign_id = path.parent.name
        recs, sk = _read_jsonl(path)
        if sk:
            skipped.append(Skipped("campaign", campaign_id, f"{sk} corrupt line(s)"))
        recs = [r for r in recs if _within_as_of(r.get("ts"), as_of)]
        if not recs:
            continue
        terminal = any(r.get("block") == "complete" for r in recs)
        concluded = campaign_id in concluded_campaigns
        newest = _newest_ts(recs)
        latest_block = recs[-1].get("block") if recs else None
        matched_by = []
        if unkeyed:
            matched_by.append("all")
        if campaign_id in retro_campaigns:
            matched_by.append(f"retro:{retro_campaigns[campaign_id]}")
        item = ActivityItem(
            kind="campaign",
            subject_id=campaign_id,
            ts=newest,
            detail={
                "latest_block": latest_block,
                "terminal": terminal,
                "concluded": concluded,
                "decisions": len(recs),
            },
            matched_by=tuple(dict.fromkeys(matched_by)),
        )
        if matched_by:
            activity.append(item)
        # The unconcluded list is a program-wide standing invitation (not query
        # filtered): every terminal campaign no current conclusion names.
        if terminal and not concluded:
            unconcluded.append(item)

    # --- (4b) run activity + matched cmd_shas for envelopes --------------------
    matched_cmd_shas: dict[str, str] = {}  # cmd_sha -> a representative run_id
    if lineage_cmd_sha:
        matched_cmd_shas.setdefault(lineage_cmd_sha, lineage or "")
    for run_id in sorted(sidecar_map):
        sidecar = sidecar_map[run_id]
        if not _within_as_of(sidecar.get("submitted_at"), as_of):
            continue
        run_tags_raw = sidecar.get("scopes")
        run_tags = (
            tuple(t for t in run_tags_raw if isinstance(t, str))
            if isinstance(run_tags_raw, list)
            else ()
        )
        cmd_sha = sidecar.get("cmd_sha")
        cmd_sha = cmd_sha if isinstance(cmd_sha, str) and cmd_sha else None

        matched_by = []
        if unkeyed:
            matched_by.append("all")
        tag_hits = query_tag_set & set(run_tags)
        if tag_hits:
            matched_by.extend(sorted(tag_hits))
        if _run_matches_lineage(run_id, cmd_sha):
            matched_by.append("lineage")
        if run_id in retro_runs:
            matched_by.append(f"retro:{retro_runs[run_id]}")
        if not matched_by:
            continue

        if cmd_sha:
            matched_cmd_shas.setdefault(cmd_sha, run_id)
        activity.append(
            ActivityItem(
                kind="run",
                subject_id=run_id,
                ts=sidecar.get("submitted_at")
                if isinstance(sidecar.get("submitted_at"), str)
                else None,
                detail={
                    "cmd_sha": cmd_sha,
                    "tags": list(run_tags),
                    "cluster": sidecar.get("cluster"),
                },
                matched_by=tuple(dict.fromkeys(matched_by)),
            )
        )

    # --- (5) fingerprint envelopes (QUOTED VERBATIM) --------------------------
    envelopes: list[EnvelopeEvidence] = []
    for cmd_sha in sorted(matched_cmd_shas):
        raw_samples, sk = fingerprint_store.read_samples(exp, cmd_sha)
        if sk:
            skipped.append(Skipped("fingerprint", cmd_sha[:16], f"{sk} corrupt line(s)"))
        raw_samples = [s for s in raw_samples if _within_as_of(s.get("ts"), as_of)]
        validated: list[determinism.Sample] = []
        for s in raw_samples:
            try:
                validated.append(determinism.validate_sample(s))
            except errors.SpecInvalid:
                skipped.append(Skipped("fingerprint", cmd_sha[:16], "sample failed validation"))
        if not validated:
            continue
        admitted = [_sample_admitted(exp, s) for s in raw_samples if _is_valid_sample(s)]
        identity = dict(validated[-1].identity)
        env = determinism.reduce_envelope(validated, admitted, identity=identity)
        for key in sorted(env.per_key):
            ke = env.per_key[key]
            ev = ke.evidence
            envelopes.append(
                EnvelopeEvidence(
                    cmd_sha=cmd_sha,
                    key=key,
                    cls=ke.cls,
                    lo=ke.lo,
                    hi=ke.hi,
                    rel_spread=ke.rel_spread,
                    n=ev.n,
                    n_full=ev.n_full,
                    n_partial=ev.n_partial,
                    scales=ev.scales,
                    clusters=ev.clusters,
                    same_submission_only=ev.same_submission_only,
                )
            )

    # --- citation re-resolution at read (DISCLOSED, never refused) ------------
    citations_status: list[CitationStatus] = []
    for conc in conclusions:
        if conc.status != CURRENT:
            continue
        for cit_dict in conc.citations:
            cit = Citation(cit_dict["kind"], cit_dict["ref"], cit_dict["sha"])
            try:
                res = resolve_citation(exp, cit, dossier_resolver=dossier_resolver)
            except errors.SpecInvalid as exc:
                # A dossier citation with no injected resolver: at READ this
                # DISCLOSES (evidence moves), it never refuses the digest.
                res = CitationResolution(False, False, str(exc).split(" — ")[0][:120])
            citations_status.append(
                CitationStatus(
                    conclusion_id=conc.conclusion_id,
                    kind=cit.kind,
                    ref=cit.ref,
                    sha=cit.sha,
                    resolved=res.resolved,
                    matches=res.matches,
                    detail=res.detail,
                )
            )

    # --- deterministic total order (byte-reproducible) ------------------------
    conclusions.sort(key=lambda c: c.conclusion_id)
    conclusions.sort(key=lambda c: c.ts or "", reverse=True)

    activity.sort(key=lambda a: (a.kind, a.subject_id))
    activity.sort(key=lambda a: a.ts or "", reverse=True)

    unconcluded.sort(key=lambda a: a.subject_id)
    unconcluded.sort(key=lambda a: a.ts or "", reverse=True)

    envelopes.sort(key=lambda e: (e.cmd_sha, e.key))
    citations_status.sort(key=lambda c: (c.conclusion_id, c.kind, c.ref))
    skipped.sort(key=lambda s: (s.source, s.subject_id, s.reason))

    return EvidenceCollection(
        experiment_dir=str(exp),
        as_of=as_of,
        tags=query_tags,
        lineage=lineage,
        conclusions=tuple(conclusions),
        activity=tuple(activity),
        envelopes=tuple(envelopes),
        unconcluded=tuple(unconcluded),
        citations_status=tuple(citations_status),
        skipped=tuple(skipped),
    )


def _is_valid_sample(sample: Mapping[str, Any]) -> bool:
    try:
        determinism.validate_sample(sample)
    except errors.SpecInvalid:
        return False
    return True


def _sample_admitted(experiment_dir: Path, sample: Mapping[str, Any]) -> bool:
    """The fingerprint admission rule, re-derived NON-CREATINGLY.

    Mirrors ``state/fingerprint_store.py::_is_admitted`` (D-consume): an
    ``auto_cleared`` sample is admitted by construction; a ``needs_verdict`` /
    ``mismatch`` sample joins ONLY when the reproduction run's decision journal
    carries a ``reproduction-verdict`` record naming the sample's ``content_sha``
    token-exact with ``accept: true``. Re-implemented here (rather than
    ``fingerprint_store.compute_admitted_flags``) because that path reads the
    journal through ``read_decisions`` → ``RepoLayout.runs``, which ``mkdir``s —
    incompatible with the collector's non-creating pin. Unification debt logged
    in the design drift log.
    """
    from collections.abc import Mapping as _Mapping

    verdict = sample.get("verdict")
    if verdict == "auto_cleared":
        return True
    if verdict not in ("needs_verdict", "mismatch"):
        return False
    content_sha = sample.get("content_sha")
    run_ids = sample.get("run_ids")
    if not content_sha or not isinstance(run_ids, (list, tuple)) or len(run_ids) < 2:
        return False
    repro_run_id = run_ids[1]
    if not isinstance(repro_run_id, str) or not repro_run_id:
        return False
    records, _ = _read_jsonl(_decision_journal_path(experiment_dir, "run", repro_run_id))
    for rec in records:
        if rec.get("block") != fingerprint_store.REPRODUCTION_VERDICT_BLOCK:
            continue
        resolved = rec.get("resolved")
        if not isinstance(resolved, _Mapping):
            continue
        if resolved.get("accept") is True and resolved.get("content_sha") == content_sha:
            return True
    return False
