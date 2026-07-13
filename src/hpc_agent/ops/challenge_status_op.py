"""``challenge-status`` — the one read-only query over standing dissent.

Design origin: ``docs/design/challenge-attestation.md`` C-verb (Wave B / T3). A
challenge is a human-authored, evidence-bound, sha-targeted attestation of
DISSENT against a committed record (C1: "a nudge is a challenge against a
proposal; a challenge is a nudge against the archive"). This verb READS that
standing state — it never files, resolves, or withdraws (those land only via
``append-decision`` under the gated ``"challenge"``-family blocks, C-gate lock 1;
this verb is ``verb="query"``, side-effect-free).

Two views, one collector:

* **the thread view** — keyed by a ``challenge_id``: the filing/verdict/withdraw
  conversation under that id, reduced to its ``open|upheld|dismissed|withdrawn|
  superseded`` status.
* **the target view** — keyed by a target ADDRESS (a ``content_sha``, or a
  ``{subject_kind, subject_id}`` pair): "what stands against this record?" — every
  standing challenge whose target names that address.

The read POSTURE is the evidence-memory E-read rule, applied to dissent
(``docs/design/evidence-memory.md``): the target is **re-resolved and DISCLOSED**
(``found-current | found-superseded`` — the collector's ``superseded`` signal) —
**never refused** (only the append gate refuses; evidence and targets legitimately
move). Each cited evidence sha is likewise re-resolved and disclosed per line
(``verified`` / not) through the ONE evidence resolver table. ``contested`` counts
ride beside — a ``current`` target reads ``current`` AND contested (C-status: an
orthogonal dimension, never a fifth status, never blocking).

The brief is CODE-rendered from the projection's own fields — dated, sha-cited,
with **no urgency / recommendation / interpretation vocabulary** (the
attention-queue D1 no-urgency rule; the token pin in the tests). Its
canonical-JSON sha is the ``view_sha`` a subsequent ``challenge-verdict`` may
carry: the render is a PURE FUNCTION of the result data (no wall-clock, no fleet
accounting), so the verdict gate RECOMPUTES a carried ``view_sha`` and it
matches byte-for-byte (the v1.6 recomputable-render precedent).

Dependencies (both landed; hard imports — the loud-import contract of
``_kernel/registry/primitive.py``):

* **``_wire/queries/challenge_status.py`` (T2)** — ``ChallengeStatusSpec`` /
  ``ChallengeStatusResult`` and the inline item models (``ChallengeEntry`` /
  ``ChallengeTarget`` / ``ChallengeVerdict`` / ``CitationStatusLine`` /
  ``ContestedCounts`` / ``SkippedNamespace``). The wire is the source of truth
  for the result shape.
* **``state/challenges.py`` (T1)** — ``standing_challenges`` (the ONE collector
  every disclosure seat routes through — C-reduce / the C-disclose enforcement
  row), returning a ``StandingChallenges`` bundle of reduced ``ChallengeStatus``
  rows per namespace. ``state`` never imports ``ops``, so the ``dossier`` resolver
  is composed HERE and injected (the evidence-brief idiom). Referenced by
  module-level name so a test monkeypatches
  ``challenge_status_op.standing_challenges``.
* **``state/evidence.py``** — ``resolve_citation``, the ONE citation resolver the
  read side re-runs to disclose each cited sha's ``verified`` status (E-read).

This file lives at the ``ops/`` role root (sibling to ``evidence_brief_op.py`` /
``export_dossier.py``); the subject-imports lint short-circuits role-root files,
so the cross-subject reads + the ``export_dossier`` composition are allowed by
construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.challenge_status import (
    ChallengeEntry,
    ChallengeStatusResult,
    ChallengeStatusSpec,
    ChallengeTarget,
    ChallengeVerdict,
    CitationStatusLine,
    ContestedCounts,
    SkippedNamespace,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops import export_dossier
from hpc_agent.ops.attention_queue import discover_fleet_experiments
from hpc_agent.state.challenges import standing_challenges
from hpc_agent.state.determinism import canonical_sha
from hpc_agent.state.evidence import resolve_citation

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = ["challenge_status"]

#: The two-way target re-resolution the collector's ``superseded`` signal
#: expresses (``TargetResolution`` also carries ``unresolvable``, which the
#: newest-wins collector folds into a non-superseded read — a superset literal).
_FOUND_CURRENT: Literal["found-current"] = "found-current"
_FOUND_SUPERSEDED: Literal["found-superseded"] = "found-superseded"


# --- the injected dossier resolver (state never imports ops) -----------------


def _dossier_resolver_for(experiment_dir: Path) -> Callable[[str], str | None]:
    """A ``ref -> bundle_sha256 | None`` resolver bound to *experiment_dir*.

    Composed HERE and injected into ``standing_challenges`` / ``resolve_citation``
    — ``state`` never imports ``ops`` (the evidence-brief drift-log item 2 seam).
    At READ an unresolvable dossier returns ``None`` → the collector/resolver
    DISCLOSES it, never raises (only the append gate refuses).
    """

    def _resolve(ref: str) -> str | None:
        try:
            sig = export_dossier.compute_dossier_signature(experiment_dir, ref)
        except Exception:  # noqa: BLE001 — read-side: any failure is "unresolvable here"
            return None
        return sig.bundle_sha256

    return _resolve


# --- collection → projection (mechanism-nouned, deterministic) ---------------


def _entry(status: Any) -> ChallengeEntry:
    """Project one collector ``ChallengeStatus`` → the wire ``ChallengeEntry``.

    Reads the T1 reduced-status contract by attribute: ``challenge_id``,
    ``status``, ``filed_at``, ``target`` (the filing's target mapping —
    ``kind`` / ``subject_kind`` / ``subject_id`` / ``content_sha``), ``superseded``
    (→ the target's read re-resolution), ``filing`` (the filing ``resolved``
    mapping — ``grounds``), and ``verdict`` / ``reasoning`` / ``resolved_at`` (the
    ruling, when present). Identity + counting only — nothing here reads
    ``grounds`` or ``reasoning`` for meaning.
    """
    target: Any = status.target or {}
    verdict: ChallengeVerdict | None = None
    verdict_val = getattr(status, "verdict", None)
    if verdict_val is not None:
        verdict = ChallengeVerdict(
            verdict=verdict_val,
            reasoning=getattr(status, "reasoning", None) or "",
            ts=getattr(status, "resolved_at", None) or "",
        )
    filing = status.filing or {}
    grounds = filing.get("grounds")
    return ChallengeEntry(
        challenge_id=status.challenge_id,
        status=status.status,
        filed_at=status.filed_at or "",
        target=ChallengeTarget(
            kind=target.get("kind"),
            subject_kind=target.get("subject_kind"),
            subject_id=target.get("subject_id"),
            content_sha=target.get("content_sha"),
        ),
        resolution=_FOUND_SUPERSEDED if status.superseded else _FOUND_CURRENT,
        grounds=grounds if isinstance(grounds, str) else "",
        verdict=verdict,
    )


def _citation_lines(experiment_dir: Path, status: Any) -> list[CitationStatusLine]:
    """Re-resolve one challenge's cited evidence → the per-citation disclosure.

    The E-read posture: each cited sha the filing recorded is re-resolved LIVE
    against *experiment_dir* through the ONE evidence resolver
    (:func:`state.evidence.resolve_citation`, the ``dossier`` resolver injected) and
    reported ``verified`` / not — DISCLOSED, never refused (only the append gate
    refuses). A malformed citation or an unknown kind is skipped, not raised.
    """
    resolver = _dossier_resolver_for(experiment_dir)
    filing = status.filing or {}
    raw_citations = filing.get("citations")
    lines: list[CitationStatusLine] = []
    if not isinstance(raw_citations, list):
        return lines
    for raw in raw_citations:
        if not isinstance(raw, dict):
            continue
        item: Any = raw
        try:
            res = resolve_citation(experiment_dir, item, dossier_resolver=resolver)
        except errors.SpecInvalid:
            continue
        lines.append(
            CitationStatusLine(
                challenge_id=status.challenge_id,
                kind=item.get("kind"),
                ref=item.get("ref"),
                sha=item.get("sha"),
                verified=res.resolved and res.matches,
            )
        )
    return lines


def _contested_counts(statuses: Sequence[Any]) -> ContestedCounts:
    """The C-status counts + ids over the SELECTED statuses (orthogonal to status).

    Counts the reduced statuses the ONE collector produced (never a re-reduction —
    the statuses ride in unchanged); the thread view filters to one id post-hoc, so
    the block is computed over the final selection here rather than read from a
    namespace-wide bundle. A target with no matching challenge yields an all-zero
    block (the wire default).
    """
    counts = {"open": 0, "upheld": 0, "dismissed": 0, "withdrawn": 0, "superseded": 0}
    for s in statuses:
        if s.status in counts:
            counts[s.status] += 1
    return ContestedCounts(
        open=counts["open"],
        upheld=counts["upheld"],
        dismissed=counts["dismissed"],
        withdrawn=counts["withdrawn"],
        superseded=counts["superseded"],
        challenge_ids=sorted(s.challenge_id for s in statuses),
    )


def _sha_prefix(sha: str) -> str:
    """The 8-hex display prefix (the R6 sha-prefix idiom); short but naming."""
    return sha[:8]


def _render(
    view: str,
    address: dict[str, Any],
    entries: Sequence[ChallengeEntry],
    contested: ContestedCounts,
    citations_by_id: dict[str, list[CitationStatusLine]],
) -> str:
    """Render the markdown brief — dated, sha-cited, mechanism-nouned.

    NO urgency / recommendation / interpretation vocabulary (the token pin): the
    brief states identities, dates, sha prefixes, and reduced statuses. ``grounds``
    and ``reasoning`` are echoed VERBATIM (opaque), never summarised. Pure function
    of its arguments — no wall-clock — so two calls render byte-identically and
    the verdict gate can recompute the ``view_sha``.
    """
    lines: list[str] = ["# challenge-status"]
    if view == "thread":
        lines.append(f"thread · challenge {address.get('challenge_id')}")
    else:
        if address.get("content_sha"):
            lines.append(f"target · content_sha {_sha_prefix(str(address['content_sha']))}")
        else:
            lines.append(f"target · {address.get('subject_kind')} · {address.get('subject_id')}")

    target_resolution = entries[0].resolution if entries else None
    if target_resolution is not None:
        lines.append(f"target re-resolution · {target_resolution}")

    if (
        contested.open
        or contested.upheld
        or contested.dismissed
        or contested.withdrawn
        or contested.superseded
    ):
        lines.append(
            "contested · "
            f"{contested.open} open · {contested.upheld} upheld · "
            f"{contested.dismissed} dismissed · {contested.withdrawn} withdrawn · "
            f"{contested.superseded} superseded"
        )

    if not entries:
        lines.append("no standing challenges name this address.")
        return "\n".join(lines)

    for e in entries:
        lines.append("")
        lines.append(f"## {e.challenge_id} · {e.status} · filed {e.filed_at}")
        lines.append(
            f"target · {e.target.kind} · {e.target.subject_kind} · {e.target.subject_id} · "
            f"sha {_sha_prefix(e.target.content_sha)} · {e.resolution}"
        )
        cited = ", ".join(
            f"{c.kind} {_sha_prefix(c.sha)} ({'verified' if c.verified else 'unresolvable here'})"
            for c in citations_by_id.get(e.challenge_id, [])
        )
        lines.append(f"cites · {cited}" if cited else "cites · (none)")
        if e.verdict is not None:
            lines.append(f"verdict · {e.verdict.verdict}")
            if e.verdict.reasoning:
                lines.append(f"reasoning · {e.verdict.reasoning}")
        lines.append(f"grounds · {e.grounds}")
    return "\n".join(lines)


# --- the primitive ------------------------------------------------------------


@primitive(
    name="challenge-status",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Read the standing dissent over a committed record. Two views: a "
            "challenge_id (the filing/verdict/withdraw thread) or a target address "
            "(a content_sha, or a subject_kind+subject_id pair) — 'what stands "
            "against this record?'. Reduces each challenge to open / upheld / "
            "dismissed / withdrawn / superseded, re-resolves the target "
            "(found-current / found-superseded) and each cited evidence sha "
            "(verified / unresolvable) — DISCLOSED, never refused. Contested is an "
            "orthogonal flag beside the target's status, never blocking. "
            "Fleet-capable. Read-only; renders a deterministic markdown brief "
            "relayed verbatim, whose canonical-JSON view_sha a verdict may bind."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ChallengeStatusSpec,
        schema_ref=SchemaRef(input="challenge_status"),
    ),
    agent_facing=True,
)
def challenge_status(*, experiment_dir: Path, spec: ChallengeStatusSpec) -> ChallengeStatusResult:
    """Project the standing challenges over a thread (by id) or a target address.

    Single-experiment by default; ``spec.fleet`` widens to every journaled
    namespace (the non-creating ``discover_fleet_experiments`` walk — a torn
    namespace is skipped and counted). Every surface routes through the ONE
    collector ``state/challenges.py::standing_challenges`` (with the dossier
    resolver injected); the target reads through the collector's ``superseded``
    signal and each citation is re-resolved and DISCLOSED (never refused — the
    E-read posture). ``contested`` rides beside the target's own status (C-status).
    The brief + ``view_sha`` are a pure function of the projection (no wall-clock,
    no fleet accounting), so the ``view_sha`` is byte-stable and the verdict gate
    recomputes it.

    Non-creating: reads only; writes nothing, scaffolds no journal.
    """
    exp = Path(experiment_dir)

    by_id = spec.challenge_id is not None
    view = "thread" if by_id else "target"

    if spec.fleet:
        experiments, ns_skipped = discover_fleet_experiments()
    else:
        experiments, ns_skipped = [exp], []

    # Collect (namespace, reduced-status) pairs so citation re-resolution runs
    # against the namespace each status was collected from (fleet-correct).
    collected: list[tuple[Path, Any]] = []
    bundle_skips: list[SkippedNamespace] = []
    for e in experiments:
        resolver = _dossier_resolver_for(e)
        if by_id:
            # Thread view: the collector has no id filter (C-reduce pins address
            # filtering only), so collect the namespace's standing challenges and
            # select the thread — still the ONE collector, never a private re-glob.
            bundle = standing_challenges(e, dossier_resolver=resolver)
            found = [s for s in bundle.statuses if s.challenge_id == spec.challenge_id]
        else:
            bundle = standing_challenges(
                e,
                content_sha=spec.content_sha,
                subject_kind=spec.subject_kind,
                subject_id=spec.subject_id,
                dossier_resolver=resolver,
            )
            found = list(bundle.statuses)
        collected.extend((e, s) for s in found)
        bundle_skips.extend(
            SkippedNamespace(ref=sk.challenge_id, reason=sk.reason) for sk in bundle.skipped
        )

    collected.sort(key=lambda es: es[1].challenge_id)
    statuses = [s for _, s in collected]
    entries = [_entry(s) for _, s in collected]
    citation_lines: list[CitationStatusLine] = []
    for e, s in collected:
        citation_lines.extend(_citation_lines(e, s))

    contested = _contested_counts(statuses)

    address = {
        "challenge_id": spec.challenge_id,
        "content_sha": spec.content_sha,
        "subject_kind": spec.subject_kind,
        "subject_id": spec.subject_id,
    }
    citations_by_id: dict[str, list[CitationStatusLine]] = {}
    for line in citation_lines:
        citations_by_id.setdefault(line.challenge_id, []).append(line)
    render = _render(view, address, entries, contested, citations_by_id)

    # view_sha over the dateless, fleet-free projection (the verdict gate
    # recomputes this — it must not depend on wall-clock or fleet state, so
    # ``computed_at`` and the fleet ``skipped`` accounting are excluded).
    projection = {
        "challenges": [entry.model_dump() for entry in entries],
        "citations_status": [line.model_dump() for line in citation_lines],
        "contested": contested.model_dump(),
    }
    view_sha = canonical_sha(projection)

    skipped = [SkippedNamespace(ref=str(s["ref"]), reason=str(s["reason"])) for s in ns_skipped]
    skipped.extend(bundle_skips)

    return ChallengeStatusResult(
        computed_at=utcnow_iso(),
        challenges=entries,
        citations_status=citation_lines,
        contested=contested,
        skipped=skipped,
        render=render,
        view_sha=view_sha,
    )
