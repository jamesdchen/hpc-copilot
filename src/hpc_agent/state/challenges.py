"""The challenge substrate — structured dissent as a standing, sha-bound record.

Design origin: ``docs/design/challenge-attestation.md`` (Wave A, T1). A
*challenge* is a human-authored, evidence-bound, sha-targeted attestation of
DISSENT against a committed record — standing (never consumed), disclosed
wherever the challenged record is cited, never blocking (C1–C5). It is one more
instance of the ONE attestation kernel (``state/attestation.py``), not a new
trust system: filing binds a ``content_sha`` over ``{target, citations}`` through
:func:`state.attestation.bind` (the gate, T5), the per-challenge reduction routes
its winner-selection through :func:`state.attestation.reduce`, and both the
target and the citations resolve through the ONE evidence-memory resolver table
(``state/evidence.py::CITATION_KINDS`` + :func:`state.evidence.resolve_citation`)
— never a parallel copy.

This module ships the T1 substrate every consumer (T2 wire, T3 verb, T5 gate,
T6 disclosure seams, T7 attention) routes through:

* The block-name constants (:data:`CHALLENGE_BLOCK` / :data:`CHALLENGE_VERDICT_BLOCK`
  / :data:`CHALLENGE_WITHDRAW_BLOCK` + :data:`CHALLENGE_BLOCK_FAMILY`) and the
  opaque :data:`SUBJECT_KIND`.
* The ``resolved`` validators (:func:`validate_target`,
  :func:`validate_challenge_resolved`, :func:`validate_verdict_resolved`,
  :func:`validate_withdraw_resolved`) + the canonical target+citations sha
  helper (:func:`challenge_content_sha`, the harness-contract form via
  :func:`state.determinism.canonical_sha`).
* Target resolution — :func:`resolve_target_existence` (the FILING check: for
  the ``attestation`` kind a SCAN of the named journal's committed records for
  the asserted sha, so a non-newest record is findable; for ``run`` /
  ``fingerprint`` / ``dossier`` a route through
  :func:`state.evidence.resolve_citation`) and :func:`resolve_target_current`
  (the newest-wins re-resolution the reduction/read use to compute
  ``superseded``). The ``dossier`` resolver is INJECTED (state never imports ops;
  the E-shape dispatch-placement rule).
* The per-challenge reduction (:func:`reduce_challenge`) —
  ``open | upheld | dismissed | withdrawn | superseded`` — PURE over an
  in-memory record list; ``superseded`` is COMPUTED (injected by the collector)
  and wins the headline while the verdict stays disclosed.
* :func:`standing_challenges` — the ONE collector every disclosure seat routes
  through: a NON-CREATING glob over the PINNED ``.hpc/challenges/*.decisions.jsonl``
  path (the ``"challenge"`` scope kind is T4; the collector globs the path
  directly here), tolerant read, reduced per id, filtered by exact target
  address, returning per-challenge statuses + the C-status ``contested``
  projection.

Pure/dependency-light: ``state`` reaches no SSH, no ``_wire``, and — the
load-bearing rule — no ``ops``. The dossier resolver is injected. ``contested``
never touches any status vocabulary (C-status): it is a parallel flag, and this
module imports no status enum from ``state/registration.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state import attestation, determinism, scopes
from hpc_agent.state.evidence import (
    CITATION_KINDS,
    KIND_ATTESTATION,
    Citation,
    resolve_citation,
    validate_citation,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "CHALLENGE_BLOCK",
    "CHALLENGE_VERDICT_BLOCK",
    "CHALLENGE_WITHDRAW_BLOCK",
    "CHALLENGE_BLOCK_FAMILY",
    "SUBJECT_KIND",
    "UPHELD",
    "DISMISSED",
    "VERDICTS",
    "OPEN",
    "WITHDRAWN",
    "SUPERSEDED",
    "STATUSES",
    "TargetAddress",
    "TargetResolution",
    "ChallengeResolved",
    "VerdictResolved",
    "WithdrawResolved",
    "ChallengeStatus",
    "Contested",
    "StandingChallenges",
    "validate_target",
    "validate_challenge_resolved",
    "validate_verdict_resolved",
    "validate_withdraw_resolved",
    "challenge_content_sha",
    "resolve_target_existence",
    "resolve_target_current",
    "reduce_challenge",
    "standing_challenges",
]

# --- the record blocks + the block family (C-shape) --------------------------

#: The filing block. ``append-decision`` under this block is the ONLY write path
#: (C-gate lock 1 — no verb, no chain, no next_block, no skill affordance). The
#: T5 gate refuses it for any ``scope_kind`` other than ``"challenge"`` and vice
#: versa. No code-writer path carries it (C3 — the attestor is ALWAYS human).
CHALLENGE_BLOCK = "challenge"

#: The verdict block (C4): a human, non-bare, mandatory-reasoning record that
#: resolves a challenge ``upheld`` or ``dismissed``. Binds no new sha (the R7
#: revoke form); a SEPARATE record from the filing (C-gate reserves the
#: resolver≠challenger constraint for later without a record-shape change).
CHALLENGE_VERDICT_BLOCK = "challenge-verdict"

#: The challenger-withdrawal block (C4): a human, non-bare, mandatory-reason
#: record. Binds no new sha; a SEPARATE record from the filing.
CHALLENGE_WITHDRAW_BLOCK = "challenge-withdraw"

#: The block family the ``"challenge"`` scope accepts — filing, verdict,
#: withdrawal form one thread under one ``challenge_id`` (C-shape).
CHALLENGE_BLOCK_FAMILY = frozenset(
    {CHALLENGE_BLOCK, CHALLENGE_VERDICT_BLOCK, CHALLENGE_WITHDRAW_BLOCK}
)

#: The opaque attestation ``subject_kind`` every challenge rides. Core never
#: interprets it; it distinguishes challenges from every other journal subject.
SUBJECT_KIND = "challenge"

# --- the verdict vocabulary (C4) ---------------------------------------------

UPHELD = "upheld"
DISMISSED = "dismissed"

#: The two verdicts a ``challenge-verdict`` record may carry.
VERDICTS = frozenset({UPHELD, DISMISSED})

# --- the per-challenge reduced-status vocabulary (C-reduce) -------------------
# ``open`` — no verdict/withdraw yet; ``upheld`` / ``dismissed`` — a verdict
# record won; ``withdrawn`` — a withdrawal won; ``superseded`` — COMPUTED: the
# target's subject moved off the challenged sha (drift-for-free, the R7
# "no revocation state machine" property mirrored for dissent).

OPEN = "open"
WITHDRAWN = "withdrawn"
SUPERSEDED = "superseded"

#: Every status the per-challenge reduction can yield.
STATUSES = frozenset({OPEN, UPHELD, DISMISSED, WITHDRAWN, SUPERSEDED})


# --- the target address (C-shape: citation-shaped + the R3 full address) ------


@dataclass(frozen=True)
class TargetAddress:
    """The challenged record's FULL ADDRESS (C-shape): citation-shaped + R3.

    ``{kind ∈ CITATION_KINDS, subject_kind, subject_id, content_sha,
    scope: {scope_kind, scope_id}}``. ``kind`` dispatches target resolution
    through the SAME resolver table evidence-memory owns; ``scope`` names which
    journal to scan for the ``attestation`` kind. ``subject_kind`` /
    ``subject_id`` are OPAQUE identity (the challenged record's), never read for
    meaning.
    """

    kind: str
    subject_kind: str
    subject_id: str
    content_sha: str
    scope_kind: str
    scope_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "content_sha": self.content_sha,
            "scope": {"scope_kind": self.scope_kind, "scope_id": self.scope_id},
        }


def validate_target(raw: Mapping[str, Any]) -> TargetAddress:
    """Validate a target mapping → :class:`TargetAddress`, or refuse loudly.

    ``kind`` must be a member of the CLOSED :data:`CITATION_KINDS`; the full
    address ``{subject_kind, subject_id, content_sha}`` plus a ``scope:
    {scope_kind, scope_id}`` are non-empty opaque strings (R3 — a bare slug is
    unaddressable and refused). Raises :class:`errors.SpecInvalid` naming the
    offending field.
    """
    from collections.abc import Mapping as _Mapping

    if not isinstance(raw, _Mapping):
        raise errors.SpecInvalid(f"challenge target: must be a mapping; got {raw!r}")
    kind = raw.get("kind")
    if kind not in CITATION_KINDS:
        raise errors.SpecInvalid(
            f"challenge target: kind {kind!r} is not one of the closed CITATION_KINDS "
            f"{sorted(CITATION_KINDS)}"
        )
    subject_kind = _require_str(raw.get("subject_kind"), what="challenge target: 'subject_kind'")
    subject_id = _require_str(raw.get("subject_id"), what="challenge target: 'subject_id'")
    content_sha = _require_str(raw.get("content_sha"), what="challenge target: 'content_sha'")
    scope = raw.get("scope")
    if not isinstance(scope, _Mapping):
        raise errors.SpecInvalid(
            f"challenge target: 'scope' must be a {{scope_kind, scope_id}} mapping "
            f"(the R3 full address names the journal); got {scope!r}"
        )
    scope_kind = _require_str(scope.get("scope_kind"), what="challenge target: scope.scope_kind")
    scope_id = _require_str(scope.get("scope_id"), what="challenge target: scope.scope_id")
    return TargetAddress(
        kind=kind,
        subject_kind=subject_kind,
        subject_id=subject_id,
        content_sha=content_sha,
        scope_kind=scope_kind,
        scope_id=scope_id,
    )


def _require_str(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise errors.SpecInvalid(f"{what} must be a non-empty string; got {value!r}")
    return value


def _validate_challenge_id(value: Any) -> str:
    """Validate a caller-authored ``challenge_id`` as a filesystem-safe slug.

    Reuses ``state/scopes.py::validate_tag`` — the ONE slug class — so a
    ``challenge_id`` is a safe path segment (its journal is
    ``.hpc/challenges/<challenge_id>.decisions.jsonl``). Never read for meaning.
    """
    if not isinstance(value, str):
        raise errors.SpecInvalid(f"challenge: challenge_id must be a string; got {value!r}")
    try:
        scopes.validate_tag(value)
    except errors.SpecInvalid as exc:
        raise errors.SpecInvalid(f"challenge: challenge_id — {exc}") from exc
    return value


# --- the canonical content sha (C-shape: over {target, citations}) -----------


def challenge_content_sha(
    target: TargetAddress, citations: Sequence[Citation | Mapping[str, Any]]
) -> str:
    """SHA-256 over the canonical JSON of ``{target address, citations list}``.

    The challenge's ``content_sha`` — the sha :func:`state.attestation.bind`
    recomputes at append (T5) so a challenge is hash-locked to WHAT it attacks
    and WHAT it rests on; neither can be asserted into existence. Uses the ONE
    harness-contract canonicalization (:func:`state.determinism.canonical_sha`),
    reused rather than a fourth local copy (the one-definition rule).
    """
    normalized_citations = [
        c.to_dict() if isinstance(c, Citation) else validate_citation(c).to_dict()
        for c in citations
    ]
    return determinism.canonical_sha(
        {"target": target.to_dict(), "citations": normalized_citations}
    )


# --- the ``resolved`` shapes (C-shape) ---------------------------------------


@dataclass(frozen=True)
class ChallengeResolved:
    """A validated FILING ``resolved`` payload (C-shape).

    * ``challenge_id`` — slug-validated caller-authored id.
    * ``target`` — the challenged record's :class:`TargetAddress`.
    * ``citations`` — NON-EMPTY (the evidence-bound rule, C3); each a :class:`Citation`.
    * ``grounds`` — opaque caller prose, stored + rendered verbatim, never parsed.
    * ``content_sha`` — the canonical target+citations sha (:func:`challenge_content_sha`).
    """

    challenge_id: str
    target: TargetAddress
    citations: tuple[Citation, ...]
    grounds: str
    content_sha: str


def validate_challenge_resolved(resolved: Mapping[str, Any]) -> ChallengeResolved:
    """Validate a filing ``resolved`` mapping → :class:`ChallengeResolved`.

    Server-side shape validation (T5 lock 2): slug ``challenge_id``; a full
    ``target`` address; NON-EMPTY ``citations`` (C3); non-empty opaque
    ``grounds``. Never interprets a subject, a citation ref, or the grounds for
    meaning. Raises :class:`errors.SpecInvalid` naming the offending element.
    """
    from collections.abc import Mapping as _Mapping

    if not isinstance(resolved, _Mapping):
        raise errors.SpecInvalid(f"challenge: resolved must be a mapping; got {resolved!r}")
    challenge_id = _validate_challenge_id(resolved.get("challenge_id"))
    raw_target = resolved.get("target")
    if not isinstance(raw_target, _Mapping):
        raise errors.SpecInvalid(
            f"challenge {challenge_id!r}: 'target' must be a full-address mapping; "
            f"got {raw_target!r}"
        )
    target = validate_target(raw_target)
    raw_citations = resolved.get("citations")
    if not isinstance(raw_citations, list) or not raw_citations:
        raise errors.SpecInvalid(
            f"challenge {challenge_id!r}: 'citations' must be a NON-EMPTY list — a challenge "
            f"MUST cite the evidence it rests on (the evidence-bound rule, C3); "
            f"got {raw_citations!r}"
        )
    citations = tuple(validate_citation(c) for c in raw_citations)
    grounds = resolved.get("grounds")
    if not isinstance(grounds, str) or not grounds:
        raise errors.SpecInvalid(
            f"challenge {challenge_id!r}: 'grounds' must be a non-empty string (the human's "
            f"free-text dissent, opaque); got {grounds!r}"
        )
    return ChallengeResolved(
        challenge_id=challenge_id,
        target=target,
        citations=citations,
        grounds=grounds,
        content_sha=challenge_content_sha(target, citations),
    )


@dataclass(frozen=True)
class VerdictResolved:
    """A validated ``challenge-verdict`` ``resolved`` payload (C4)."""

    challenge_id: str
    verdict: str
    reasoning: str


def validate_verdict_resolved(resolved: Mapping[str, Any]) -> VerdictResolved:
    """Validate a verdict ``resolved`` mapping → :class:`VerdictResolved`, or refuse.

    ``challenge_id`` a slug; ``verdict`` ∈ :data:`VERDICTS`; ``reasoning`` a
    mandatory non-empty free-text string (dismissal is effortful by construction,
    C4). Raises :class:`errors.SpecInvalid`.
    """
    from collections.abc import Mapping as _Mapping

    if not isinstance(resolved, _Mapping):
        raise errors.SpecInvalid(f"challenge-verdict: resolved must be a mapping; got {resolved!r}")
    challenge_id = _validate_challenge_id(resolved.get("challenge_id"))
    verdict = resolved.get("verdict")
    if verdict not in VERDICTS:
        raise errors.SpecInvalid(
            f"challenge-verdict {challenge_id!r}: 'verdict' must be one of {sorted(VERDICTS)}; "
            f"got {verdict!r}"
        )
    reasoning = resolved.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        raise errors.SpecInvalid(
            f"challenge-verdict {challenge_id!r}: 'reasoning' must be a non-empty string "
            f"(waving dissent away with a bare ack is the asymmetry violation, C4); "
            f"got {reasoning!r}"
        )
    return VerdictResolved(challenge_id=challenge_id, verdict=verdict, reasoning=reasoning)


@dataclass(frozen=True)
class WithdrawResolved:
    """A validated ``challenge-withdraw`` ``resolved`` payload (C4)."""

    challenge_id: str
    reason: str


def validate_withdraw_resolved(resolved: Mapping[str, Any]) -> WithdrawResolved:
    """Validate a withdrawal ``resolved`` mapping → :class:`WithdrawResolved`, or refuse.

    ``challenge_id`` a slug; ``reason`` a mandatory non-empty free-text string
    (the R7 revoke form). Raises :class:`errors.SpecInvalid`.
    """
    from collections.abc import Mapping as _Mapping

    if not isinstance(resolved, _Mapping):
        raise errors.SpecInvalid(
            f"challenge-withdraw: resolved must be a mapping; got {resolved!r}"
        )
    challenge_id = _validate_challenge_id(resolved.get("challenge_id"))
    reason = resolved.get("reason")
    if not isinstance(reason, str) or not reason:
        raise errors.SpecInvalid(
            f"challenge-withdraw {challenge_id!r}: 'reason' must be a non-empty string "
            f"(the mandatory withdrawal reason, C4); got {reason!r}"
        )
    return WithdrawResolved(challenge_id=challenge_id, reason=reason)


# --- target resolution (dispatch to the evidence resolver table) -------------


@dataclass(frozen=True)
class TargetResolution:
    """The result of resolving a target against the live stores (mirrors
    :class:`state.evidence.CitationResolution`).

    * ``resolved`` — the target's subject was FOUND on this namespace.
    * ``matches`` — the asserted ``content_sha`` is the subject's CURRENT
      (newest) answer.
    * ``detail`` — a short human-facing reason (disclosed in the read digest).
    """

    resolved: bool
    matches: bool
    detail: str = ""


def _target_as_citation(target: TargetAddress) -> Citation:
    """The target as a :class:`Citation` for the evidence resolver dispatch.

    ``run`` / ``fingerprint`` / ``dossier`` key on ``subject_id`` as ``ref``; the
    generic ``attestation`` kind addresses a journal as ``"<scope_kind>:<scope_id>"``
    (the :func:`state.evidence._resolve_attestation` ref shape).
    """
    if target.kind == KIND_ATTESTATION:
        ref = f"{target.scope_kind}:{target.scope_id}"
    else:
        ref = target.subject_id
    return Citation(kind=target.kind, ref=ref, sha=target.content_sha)


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Non-creating tolerant read of a JSONL file → ``(records, skipped)``.

    The ``state/evidence.py::_read_jsonl`` idiom — no ``RepoLayout`` (whose
    ``.hpc`` property ``mkdir``s), so the collector never creates a directory
    (the non-creating pin).
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


def _target_journal_path(experiment_dir: Path, scope_kind: str, scope_id: str) -> Path:
    """The decision-journal path for a target's scope — NON-CREATING.

    Mirrors ``state/evidence.py::_decision_journal_path`` (built by hand so no
    ``RepoLayout`` helper ``mkdir``s on access). The ``"challenge"`` branch is a
    ``# T4 seam:`` — the scope kind lands in ``state/decision_journal.py`` later;
    the PATH is pinned here now.
    """
    hpc = experiment_dir / ".hpc"
    if scope_kind == "run":
        return hpc / "runs" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "scope":
        return hpc / "scopes" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "notebook":
        return hpc / "notebooks" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "conclusion":
        return hpc / "conclusions" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "registration":
        return hpc / "registrations" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "pack":
        return hpc / "packs" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "challenge":
        # T4 seam: the "challenge" scope kind lands in decision_journal later;
        # the PATH is pinned here now. Non-creating.
        return hpc / "challenges" / f"{scope_id}.decisions.jsonl"
    # scope_kind == "campaign"
    return hpc / "campaigns" / scope_id / "decisions.jsonl"


def resolve_target_existence(
    experiment_dir: Path,
    target: TargetAddress,
    *,
    dossier_resolver: Callable[[str], str | None] | None = None,
) -> TargetResolution:
    """Resolve target EXISTENCE at FILING — you cannot contest what code cannot find.

    For the :data:`~state.evidence.KIND_ATTESTATION` kind this SCANS the named
    journal's committed records for the asserted ``content_sha`` — the existence
    check cannot route through the newest-wins ``reduce`` alone, which by
    construction never surfaces a non-newest record (challenging a superseded
    record is PERMITTED, C2). For ``run`` / ``fingerprint`` / ``dossier`` it
    routes through :func:`state.evidence.resolve_citation`: those subjects hold
    no non-newest record, so existence == the citation resolving AND matching.

    A target the machine cannot resolve is refused at filing by the caller (T5) —
    the R3 rejection working, not a gap to paper over. The ``dossier`` resolver
    is INJECTED (state never imports ops).
    """
    if target.kind == KIND_ATTESTATION:
        records, _ = _read_jsonl(
            _target_journal_path(experiment_dir, target.scope_kind, target.scope_id)
        )
        if not records:
            return TargetResolution(False, False, "named journal is empty or absent")
        for rec in records:
            resolved = rec.get("resolved")
            content_sha = resolved.get("content_sha") if isinstance(resolved, dict) else None
            if content_sha == target.content_sha:
                return TargetResolution(True, True, "committed record found at the asserted sha")
        return TargetResolution(
            True, False, "no committed record in the named journal carries the asserted content_sha"
        )
    res = resolve_citation(
        experiment_dir, _target_as_citation(target), dossier_resolver=dossier_resolver
    )
    return TargetResolution(res.resolved, res.matches, res.detail)


def resolve_target_current(
    experiment_dir: Path,
    target: TargetAddress,
    *,
    dossier_resolver: Callable[[str], str | None] | None = None,
) -> TargetResolution:
    """Re-resolve the target's subject NEWEST-WINS — the ``superseded`` input.

    Routes through :func:`state.evidence.resolve_citation` for EVERY kind (the
    ``attestation`` kind reduces the journal newest-wins). ``matches`` True → the
    challenged sha is still the subject's newest (not superseded); ``resolved``
    True and ``matches`` False → the subject moved off the challenged sha
    (``superseded`` by construction, C-reduce); ``resolved`` False → the subject
    is unresolvable here (cannot prove supersession — the reduction keeps its
    verdict-based status). The ``dossier`` resolver is INJECTED.
    """
    res = resolve_citation(
        experiment_dir, _target_as_citation(target), dossier_resolver=dossier_resolver
    )
    return TargetResolution(res.resolved, res.matches, res.detail)


# --- the per-challenge reduction (route-through the ONE kernel) --------------


@dataclass(frozen=True)
class ChallengeStatus:
    """The reduced status of one ``challenge_id`` (C-reduce).

    * ``status`` — :data:`OPEN` / :data:`UPHELD` / :data:`DISMISSED` /
      :data:`WITHDRAWN` / :data:`SUPERSEDED`. ``superseded`` wins the headline
      whenever it applies; the underlying verdict/withdrawal is still DISCLOSED
      in :attr:`verdict` / :attr:`withdrawn_reason` (C-reduce: "the projection
      reports both").
    * ``target`` — the filing's target mapping (the address disclosure seats filter on).
    * ``filing`` — the winning filing record's ``resolved`` mapping, or ``None``.
    * ``filed_at`` — the filing record's journal ``ts``, or ``None`` (the item ages).
    * ``content_sha`` — the filing's canonical content sha, or ``None``.
    * ``verdict`` — ``upheld`` / ``dismissed`` when a verdict record won, else ``None``.
    * ``reasoning`` — the winning verdict's reasoning, or ``None``.
    * ``withdrawn_reason`` — the winning withdrawal's reason, or ``None``.
    * ``resolved_at`` — the winning verdict/withdrawal record's ``ts``, or ``None``.
    * ``superseded`` — the COMPUTED supersession flag (the injected re-resolution).
    """

    challenge_id: str
    status: str
    target: Mapping[str, Any] | None
    filing: Mapping[str, Any] | None
    filed_at: str | None
    content_sha: str | None
    verdict: str | None = None
    reasoning: str | None = None
    withdrawn_reason: str | None = None
    resolved_at: str | None = None
    superseded: bool = False


def _resolved_of(record: Mapping[str, Any]) -> Mapping[str, Any]:
    from collections.abc import Mapping as _Mapping

    resolved = record.get("resolved")
    return resolved if isinstance(resolved, _Mapping) else {}


def _project_filing(record: Mapping[str, Any], challenge_id: str) -> dict[str, Any] | None:
    """Project a FILING record to an attestation dict, or ``None``.

    ``None`` for any record that is not a :data:`CHALLENGE_BLOCK` record for
    *challenge_id*. ``content_sha`` is recomputed from the ``resolved``
    target+citations (so the reduction is PURE over the record list); a malformed
    ``resolved`` falls back to a stored ``content_sha`` (the tolerant-read idiom,
    the ``state/evidence.py::_project_conclusion`` form).
    """
    if record.get("block") != CHALLENGE_BLOCK:
        return None
    resolved = _resolved_of(record)
    if resolved.get("challenge_id") != challenge_id:
        return None
    try:
        content_sha: Any = validate_challenge_resolved(resolved).content_sha
    except errors.SpecInvalid:
        content_sha = resolved.get("content_sha")
    return {
        "attestor": "human",
        "subject_kind": SUBJECT_KIND,
        "subject_id": challenge_id,
        "content_sha": content_sha,
    }


def reduce_challenge(
    records: Sequence[Mapping[str, Any]],
    *,
    challenge_id: str,
    superseded: bool = False,
) -> ChallengeStatus:
    """Reduce a challenge_id's records to a :class:`ChallengeStatus` (C-reduce).

    PURE over an in-memory *records* list in APPEND (chronological) order —
    newest last, the order ``decision_journal.read_decisions`` returns. Adds ONLY
    winner-selection (the ``state/registration.py::reduce_registration`` form) on
    top of the ONE kernel: the filing's currency verdict routes through
    :func:`state.attestation.reduce`, NEVER a re-inlined newest-first or
    sha-compare (the enforcement-map "one kernel" row).

    * :data:`OPEN` — a filing exists and no verdict/withdrawal record has won.
    * :data:`UPHELD` / :data:`DISMISSED` — the newest resolution record is a
      verdict carrying that verdict.
    * :data:`WITHDRAWN` — the newest resolution record is a withdrawal.
    * :data:`SUPERSEDED` — *superseded* is True (the injected re-resolution found
      the target's subject moved off the challenged sha, C-reduce). Wins the
      headline regardless of verdicts; the underlying verdict/withdrawal stays
      disclosed.

    *superseded* is COMPUTED by the collector (:func:`standing_challenges`) via
    :func:`resolve_target_current` and injected here so the reduction stays pure.
    Malformed records are skipped.
    """
    filing_records: list[Mapping[str, Any]] = []
    resolution_winner: Mapping[str, Any] | None = None
    for record in records:
        block = record.get("block")
        if block not in CHALLENGE_BLOCK_FAMILY:
            continue
        if _resolved_of(record).get("challenge_id") != challenge_id:
            continue
        if block == CHALLENGE_BLOCK:
            filing_records.append(record)
        else:
            resolution_winner = record  # append order → the last is the newest

    filing_winner = filing_records[-1] if filing_records else None
    filing_resolved = _resolved_of(filing_winner) if filing_winner is not None else None
    target = filing_resolved.get("target") if filing_resolved else None
    filed_at = filing_winner.get("ts") if filing_winner is not None else None
    filed_at = filed_at if isinstance(filed_at, str) else None

    # Route the filing's currency verdict through the ONE kernel (never re-inline
    # the newest-first pick). The result mirrors reduce_conclusion: a filing that
    # exists is a live challenge; the value confirms the projection routed the
    # kernel (the one-kernel enforcement pin).
    projected = [
        p for p in (_project_filing(r, challenge_id) for r in filing_records) if p is not None
    ]
    filing_projection = _project_filing(filing_winner, challenge_id) if filing_winner else None
    filing_sha_any = filing_projection.get("content_sha") if filing_projection else None
    content_sha = filing_sha_any if isinstance(filing_sha_any, str) else None
    attestation.reduce(
        projected,
        current_sha=content_sha if content_sha is not None else "",
        subject_id=challenge_id,
    )

    verdict: str | None = None
    reasoning: str | None = None
    withdrawn_reason: str | None = None
    resolved_at: str | None = None
    base_status = OPEN
    if resolution_winner is not None:
        block = resolution_winner.get("block")
        rw_resolved = _resolved_of(resolution_winner)
        rts = resolution_winner.get("ts")
        resolved_at = rts if isinstance(rts, str) else None
        if block == CHALLENGE_WITHDRAW_BLOCK:
            base_status = WITHDRAWN
            reason = rw_resolved.get("reason")
            withdrawn_reason = reason if isinstance(reason, str) else None
        elif block == CHALLENGE_VERDICT_BLOCK:
            raw_verdict = rw_resolved.get("verdict")
            if raw_verdict in VERDICTS:
                verdict = raw_verdict
                base_status = raw_verdict
            reason = rw_resolved.get("reasoning")
            reasoning = reason if isinstance(reason, str) else None

    headline = SUPERSEDED if superseded else base_status
    return ChallengeStatus(
        challenge_id=challenge_id,
        status=headline,
        target=target if isinstance(target, dict) else None,
        filing=filing_resolved,
        filed_at=filed_at,
        content_sha=content_sha,
        verdict=verdict,
        reasoning=reasoning,
        withdrawn_reason=withdrawn_reason,
        resolved_at=resolved_at,
        superseded=superseded,
    )


# --- the C-status ``contested`` projection + the ONE collector ---------------


@dataclass(frozen=True)
class Contested:
    """The C-status projection every disclosure seat carries — counts + identities.

    ``contested: {open, upheld, dismissed, withdrawn, superseded, challenge_ids}``
    — never a severity score (the D1 no-urgency rule). A target with all-zero
    counts OMITS the block: :func:`standing_challenges` returns ``None`` for
    :attr:`StandingChallenges.contested` when no challenge matches (the
    emitted-only-when-present precedent).
    """

    open: int
    upheld: int
    dismissed: int
    withdrawn: int
    superseded: int
    challenge_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "open": self.open,
            "upheld": self.upheld,
            "dismissed": self.dismissed,
            "withdrawn": self.withdrawn,
            "superseded": self.superseded,
            "challenge_ids": list(self.challenge_ids),
        }


@dataclass(frozen=True)
class Skipped:
    """A disclosed collection gap — a corrupt line in a challenge journal."""

    challenge_id: str
    reason: str


@dataclass(frozen=True)
class StandingChallenges:
    """The ONE collector's output (C-reduce): per-challenge statuses + ``contested``.

    * ``statuses`` — the reduced :class:`ChallengeStatus` of every challenge whose
      target address matched the query, ``challenge_id``-sorted.
    * ``contested`` — the C-status projection, or ``None`` when no challenge
      matched (the all-zero omission).
    * ``skipped`` — disclosed corrupt-line gaps.
    """

    experiment_dir: str
    statuses: tuple[ChallengeStatus, ...]
    contested: Contested | None
    skipped: tuple[Skipped, ...]


def standing_challenges(
    experiment_dir: Path | str,
    *,
    content_sha: str | None = None,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    dossier_resolver: Callable[[str], str | None] | None = None,
) -> StandingChallenges:
    """Collect standing challenges under one namespace → :class:`StandingChallenges`.

    The ONE definition every disclosure seat routes through (C-reduce; the
    attention-queue D5 discipline). A NON-CREATING glob over the PINNED
    ``.hpc/challenges/*.decisions.jsonl`` path (the ``"challenge"`` scope kind is
    T4; the collector globs the path directly here), tolerant read, reduced per
    ``challenge_id`` (:func:`reduce_challenge`), filtered by EXACT target address.

    The ``superseded`` input is computed per challenge via
    :func:`resolve_target_current` (the target re-resolves newest-wins through the
    evidence resolver table; the ``dossier`` resolver is INJECTED). Matching keys
    on the target's ``content_sha`` (exact — the full address's discriminator);
    ``subject_kind`` / ``subject_id`` narrow it (all supplied filters AND). With
    no filter every challenge is returned (the fleet/attention view).

    Non-creating: reads only; a fresh namespace yields empty and creates nothing.
    """
    exp = Path(experiment_dir)
    hpc = exp / ".hpc"

    records_by_id: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    skipped: list[Skipped] = []
    for path in sorted(hpc.glob("challenges/*.decisions.jsonl")):
        recs, sk = _read_jsonl(path)
        cid_from_name = path.name[: -len(".decisions.jsonl")]
        if sk:
            skipped.append(Skipped(cid_from_name, f"{sk} corrupt line(s)"))
        for r in recs:
            cid = _resolved_of(r).get("challenge_id")
            if not isinstance(cid, str) or not cid:
                continue
            if cid not in records_by_id:
                records_by_id[cid] = []
                order.append(cid)
            records_by_id[cid].append(r)

    statuses: list[ChallengeStatus] = []
    for cid in order:
        recs = records_by_id[cid]
        # A filing must exist for the id to be a challenge (a stray verdict alone
        # is not addressable). Compute supersession from the filing's target.
        filing_target: TargetAddress | None = None
        for r in recs:
            if r.get("block") != CHALLENGE_BLOCK:
                continue
            try:
                filing_target = validate_challenge_resolved(_resolved_of(r)).target
            except errors.SpecInvalid:
                filing_target = None
        if filing_target is None:
            continue

        # Address filter (exact) — the full address's discriminators.
        if content_sha is not None and filing_target.content_sha != content_sha:
            continue
        if subject_kind is not None and filing_target.subject_kind != subject_kind:
            continue
        if subject_id is not None and filing_target.subject_id != subject_id:
            continue

        res = resolve_target_current(exp, filing_target, dossier_resolver=dossier_resolver)
        is_superseded = res.resolved and not res.matches
        statuses.append(reduce_challenge(recs, challenge_id=cid, superseded=is_superseded))

    statuses.sort(key=lambda s: s.challenge_id)

    contested = _contested_of(statuses)
    return StandingChallenges(
        experiment_dir=str(exp),
        statuses=tuple(statuses),
        contested=contested,
        skipped=tuple(skipped),
    )


def _contested_of(statuses: Sequence[ChallengeStatus]) -> Contested | None:
    """Build the C-status ``contested`` projection, or ``None`` when all-zero.

    Counts each status and collects identities; a target with no matching
    challenge OMITS the block (returns ``None``) — the emitted-only-when-present
    precedent. ``contested`` NEVER touches any status vocabulary (C-status): it is
    a parallel flag built here from the challenge statuses alone.
    """
    if not statuses:
        return None
    counts = {OPEN: 0, UPHELD: 0, DISMISSED: 0, WITHDRAWN: 0, SUPERSEDED: 0}
    for s in statuses:
        if s.status in counts:
            counts[s.status] += 1
    return Contested(
        open=counts[OPEN],
        upheld=counts[UPHELD],
        dismissed=counts[DISMISSED],
        withdrawn=counts[WITHDRAWN],
        superseded=counts[SUPERSEDED],
        challenge_ids=tuple(sorted(s.challenge_id for s in statuses)),
    )
