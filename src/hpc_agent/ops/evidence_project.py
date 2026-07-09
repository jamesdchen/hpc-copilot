"""Shared collection→wire projection helpers for the two evidence query verbs.

``evidence-brief`` (the POINT query, ``ops/evidence_brief_op.py``) and
``evidence-period`` (the WINDOW projection, ``ops/evidence_period_op.py``) both
project the ONE collector's :class:`~hpc_agent.state.evidence.EvidenceCollection`
onto the ``_wire`` result shapes. The T5/T6 agents flagged (and Wave C's T12
drift log recorded) that their collection→wire helpers were duplicated; this
module extracts the *genuinely byte-identical* subset so there is ONE definition.

Only the pieces that were byte-for-byte the same across both verbs live here:

* :func:`project_envelope_lines` — the per-key determinism-envelope projection.
  The :class:`~hpc_agent._wire.queries.evidence.EnvelopeLine` construction was
  identical in both verbs; the ONLY difference was the envelope *string*
  formatter (brief renders ``±..% rel`` at 2 decimals, period at 1), so the
  formatter is INJECTED (``fmt``) and stays local to each verb.
* :func:`apply_evidence_order` — the fleet-merge total order. Both verbs' merge
  helpers re-sorted the unioned lists with the exact same nine ``.sort()`` calls
  (``collect_evidence``'s documented total order); that block is extracted here.
  The surrounding merge logic (brief always merges + carries scope params;
  period short-circuits a single namespace + derives them) genuinely differs and
  stays local.

Deliberately NOT extracted (each verb's genuinely-different pieces, kept local so
behavior stays byte-identical): ``_conclusion_lines`` (brief truncates the cited
sha to 8 chars; period keeps the full sha and drops empties), ``_activity_lines``
(two different roll-up algorithms), ``_citation_*`` (brief's ``verified`` is
``resolved and matches``; period's is ``matches`` alone), and the envelope
formatters themselves.

Lives at the ``ops/`` role root (sibling to the two verb modules); imports the
``state`` collection types and the ``_wire`` result types only — no SSH, no
scheduler, no side effects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._wire.queries.evidence import EnvelopeLine

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from hpc_agent.state.evidence import EnvelopeEvidence

__all__ = ["project_envelope_lines", "apply_evidence_order"]


def project_envelope_lines(
    envelopes: Sequence[EnvelopeEvidence],
    fmt: Callable[[EnvelopeEvidence], str],
) -> list[EnvelopeLine]:
    """Project each per-key determinism envelope → an :class:`EnvelopeLine`.

    The evidence LABELS (``n`` / ``n_full`` / ``n_partial`` / ``scales`` /
    ``clusters``) ride the wire QUOTED VERBATIM from the collector's own
    reduction — never recomputed or reinterpreted. *fmt* composes the compact
    envelope string from one envelope (the only piece that differs between the
    two verbs, so it is injected and stays local to each).
    """
    return [
        EnvelopeLine(
            lineage=e.cmd_sha,
            envelope=fmt(e),
            n=e.n,
            n_full=e.n_full,
            n_partial=e.n_partial,
            scales=list(e.scales),
            clusters=list(e.clusters),
        )
        for e in envelopes
    ]


def apply_evidence_order(
    *,
    conclusions: list[Any],
    activity: list[Any],
    unconcluded: list[Any],
    envelopes: list[Any],
    citations_status: list[Any],
    skipped: list[Any],
) -> None:
    """Re-establish ``collect_evidence``'s documented total order, IN PLACE.

    The fleet merge unions per-namespace projections; this restores the exact
    same total order a single-namespace collection carries, so a fleet digest is
    as byte-reproducible as one namespace. Python's sort is stable, so the paired
    ``.sort()`` calls per list are order-significant and preserved verbatim.
    Mutates the six lists in place (mirrors the merge helpers' prior inline block).
    """
    conclusions.sort(key=lambda c: c.conclusion_id)
    conclusions.sort(key=lambda c: c.ts or "", reverse=True)
    activity.sort(key=lambda a: (a.kind, a.subject_id))
    activity.sort(key=lambda a: a.ts or "", reverse=True)
    unconcluded.sort(key=lambda a: a.subject_id)
    unconcluded.sort(key=lambda a: a.ts or "", reverse=True)
    envelopes.sort(key=lambda e: (e.cmd_sha, e.key))
    citations_status.sort(key=lambda c: (c.conclusion_id, c.kind, c.ref))
    skipped.sort(key=lambda s: (s.source, s.subject_id, s.reason))
