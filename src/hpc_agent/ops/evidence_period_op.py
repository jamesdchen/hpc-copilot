"""``evidence-period`` — the WINDOW projection over the one evidence collector.

A read-only ``verb="query"`` primitive (``docs/design/evidence-memory.md`` E5,
Wave B / T6; the ``run-story`` / ``attention-queue`` pure-projection posture).
Given a ``since`` (required lower bound) and an optional ``until`` (inclusive
upper bound, default open/now), it projects the SAME collector every evidence
surface calls (``state/evidence.py::collect_evidence``) over a time window and
renders a dated timeline that ENDS with the unconcluded-campaigns list — the
place the conclusion loop closes.

**Window semantics (as landed).** The collector OWNS the upper bound: it is
called with ``as_of=until`` so every store is time-filtered to ``ts <= until``
deterministically, in one definition. The LOWER bound is a projection filter
kept HERE in the verb (``_window_filter``): after collection, conclusions,
activity and the unconcluded list are kept only when ``ts >= since``. Splitting
the window this way keeps ``as_of`` a single collector concern (reused by every
surface) while ``since`` stays a cheap deterministic post-filter — no second
walk, no re-reduction (the one-collector enforcement row).

Pure projection: no SSH, no scheduler, no write, no store — recomputed on every
call from the on-disk records (the content-keyed ``state/evidence_cache.py`` only
MEMOIZES the recompute; deleting it changes no output). It never interprets what
a tag means or what a ``finding`` says (both opaque, echoed, never parsed) — the
Q1 boundary the ``test_evidence_boundary`` suite patrols.

This file lives at the ``ops/`` *role root* (sibling to ``run_story.py`` /
``attention_op.py``) because it composes the cross-subject ``state`` collector,
the ``state`` cache, the ``ops`` fleet discovery, and the ``ops`` render seam.

The digest render is the sibling ``ops/evidence_render.py::render_period`` seam,
imported LATE (inside :func:`_render_period`) so this module imports cleanly
before that parallel Wave-B file lands and so tests can stub it by injecting a
``hpc_agent.ops.evidence_render`` module into ``sys.modules``.

Shared-helper seam (T12 follow-up, DONE): the genuinely byte-identical pieces of
the collection→wire projection were extracted to ``ops/evidence_project.py`` — the
envelope-line loop (:func:`_envelope_lines` now injects its own formatter into
``project_envelope_lines``) and the fleet total-order sort (:func:`_merge_collections`
calls ``apply_evidence_order``). The genuinely-different pieces stay local:
:func:`_conclusion_lines` (full sha, empties dropped — brief truncates to 8),
:func:`_activity_lines` (a different roll-up), :func:`_citation_lines`
(``verified = matches`` — brief ANDs ``resolved``), :func:`_unconcluded_items`,
and the merge's window/param derivation.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.evidence import (
    ActivityLine,
    CitationStatusLine,
    ConclusionLine,
    EnvelopeLine,
    EvidencePeriodResult,
    EvidencePeriodSpec,
    SkippedNamespace,
    UnconcludedItem,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.attention_queue import discover_fleet_experiments
from hpc_agent.ops.evidence_project import apply_evidence_order, project_envelope_lines
from hpc_agent.state import evidence_cache
from hpc_agent.state.evidence import (
    CURRENT,
    EvidenceCollection,
    collect_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["evidence_period"]


# --- the window lower-bound filter (kept in the verb; as_of is the collector's) --


def _ts_ge(ts: Any, since: str) -> bool:
    """Inclusive lower-bound filter: ``ts >= since`` (a usable string ts only).

    A record with no usable ``ts`` cannot be shown to fall in the window, so it
    is EXCLUDED (disclosed by its absence, never fabricated in) — the mirror of
    the collector's ``_within_as_of`` upper-bound rule.
    """
    return isinstance(ts, str) and bool(ts) and ts >= since


def _window_filter(collection: EvidenceCollection, *, since: str) -> EvidenceCollection:
    """Apply the ``since`` lower bound to an already-``as_of``-collected projection.

    Conclusions, activity and the unconcluded list are kept only when their
    ``ts >= since``; citation-status rows are kept for the conclusions that
    survive; envelopes pass through (a determinism envelope is a per-lineage
    reduction with no single ts — it already reflects the ``as_of=until`` cut the
    collector applied). Order is preserved (the collector's total order is
    stable under a filter), so the render stays byte-reproducible.
    """
    conclusions = tuple(c for c in collection.conclusions if _ts_ge(c.ts, since))
    surviving_ids = {c.conclusion_id for c in conclusions}
    activity = tuple(a for a in collection.activity if _ts_ge(a.ts, since))
    unconcluded = tuple(a for a in collection.unconcluded if _ts_ge(a.ts, since))
    citations_status = tuple(
        cs for cs in collection.citations_status if cs.conclusion_id in surviving_ids
    )
    return replace(
        collection,
        conclusions=conclusions,
        activity=activity,
        unconcluded=unconcluded,
        citations_status=citations_status,
    )


# --- fleet: merge per-namespace collections into one for the render seam ------


def _merge_collections(collections: Sequence[EvidenceCollection]) -> EvidenceCollection:
    """Concatenate per-namespace collections and re-establish the total order.

    Each namespace was collected with the same ``as_of``/``tags``; fleet mode
    unions their projections and re-sorts on the collector's documented keys so
    the fleet render is one deterministic timeline. ``experiment_dir`` becomes a
    ``"<fleet>"`` marker (identity, not a path). Empty input → an empty
    collection (fleet over zero discovered namespaces is data, not an error).
    """
    if len(collections) == 1:
        return collections[0]

    conclusions: list[Any] = []
    activity: list[Any] = []
    envelopes: list[Any] = []
    unconcluded: list[Any] = []
    citations_status: list[Any] = []
    skipped: list[Any] = []
    for c in collections:
        conclusions.extend(c.conclusions)
        activity.extend(c.activity)
        envelopes.extend(c.envelopes)
        unconcluded.extend(c.unconcluded)
        citations_status.extend(c.citations_status)
        skipped.extend(c.skipped)

    apply_evidence_order(
        conclusions=conclusions,
        activity=activity,
        unconcluded=unconcluded,
        envelopes=envelopes,
        citations_status=citations_status,
        skipped=skipped,
    )

    first = collections[0] if collections else None
    return EvidenceCollection(
        experiment_dir="<fleet>",
        as_of=first.as_of if first is not None else None,
        tags=first.tags if first is not None else (),
        lineage=None,
        conclusions=tuple(conclusions),
        activity=tuple(activity),
        envelopes=tuple(envelopes),
        unconcluded=tuple(unconcluded),
        citations_status=tuple(citations_status),
        skipped=tuple(skipped),
    )


# --- collection → wire projection (shared-in-spirit with evidence-brief; see NOTE) --


def _conclusion_lines(collection: EvidenceCollection) -> list[ConclusionLine]:
    lines: list[ConclusionLine] = []
    for c in collection.conclusions:
        lines.append(
            ConclusionLine(
                conclusion_id=c.conclusion_id,
                ts=c.ts or "",
                tags=list(c.tags),
                cited_shas=[cit.get("sha", "") for cit in c.citations if cit.get("sha")],
                status=c.status,  # type: ignore[arg-type]  # collector emits a ConclusionStatus member
                finding=c.finding,
            )
        )
    return lines


def _activity_lines(collection: EvidenceCollection) -> list[ActivityLine]:
    """Per-tag prior-work counts — identity + counting over the flat activity list.

    The collector emits a flat, uniform activity projection (``kind`` ∈
    ``{"tag", "campaign", "run"}``); this rolls the per-tag view up for the wire:
    ``runs`` counts run rows carrying the tag, ``lineages`` their distinct
    ``cmd_sha`` (falling back to the tag ledger's ``distinct_lineages``),
    ``looks`` the tag ledger's ``prior_looks``, ``campaigns`` the campaign rows a
    current conclusion carrying this tag retro-indexes (pure identity). Every
    field is a COUNT or a DATE — no ranking, no judgment (the E-render rule).
    """
    conc_tags: dict[str, set[str]] = {
        c.conclusion_id: set(c.tags) for c in collection.conclusions if c.status == CURRENT
    }
    run_items = [a for a in collection.activity if a.kind == "run"]
    campaign_items = [a for a in collection.activity if a.kind == "campaign"]
    tag_items = [a for a in collection.activity if a.kind == "tag"]

    lines: list[ActivityLine] = []
    for item in tag_items:
        tag = item.subject_id
        runs = [r for r in run_items if tag in (r.detail.get("tags") or [])]
        run_cmd_shas = {r.detail.get("cmd_sha") for r in runs if r.detail.get("cmd_sha")}
        cids_with_tag = {cid for cid, tset in conc_tags.items() if tag in tset}
        campaigns = [
            c
            for c in campaign_items
            if any(
                mb.startswith("retro:") and mb.split(":", 1)[1] in cids_with_tag
                for mb in c.matched_by
            )
        ]
        lineages = len(run_cmd_shas) or int(item.detail.get("distinct_lineages") or 0)
        lines.append(
            ActivityLine(
                tag=tag,
                campaigns=len(campaigns),
                runs=len(runs),
                lineages=lineages,
                looks=int(item.detail.get("prior_looks") or 0),
                newest=item.ts,
            )
        )
    lines.sort(key=lambda a: a.tag)
    return lines


def _fmt_envelope(e: Any) -> str:
    """A minimal, mechanical envelope string for the wire field.

    Number formatting only (``±<rel>% rel`` / an ``[lo, hi]`` interval / the
    class label) — never interpretation prose. The RICH, human-facing envelope
    render belongs to ``ops/evidence_render.py``; this is the terse wire echo
    (shared-helper seam flagged in the module NOTE).
    """
    if e.rel_spread is not None:
        return f"±{e.rel_spread * 100:.1f}% rel"
    if e.lo is not None and e.hi is not None:
        return f"[{e.lo}, {e.hi}]"
    return str(e.cls)


def _envelope_lines(collection: EvidenceCollection) -> list[EnvelopeLine]:
    """Shared loop (``ops/evidence_project.py``); the period's own 1-decimal
    formatter is injected and stays local."""
    return project_envelope_lines(collection.envelopes, _fmt_envelope)


def _citation_lines(collection: EvidenceCollection) -> list[CitationStatusLine]:
    return [
        CitationStatusLine(
            conclusion_id=cs.conclusion_id,
            kind=cs.kind,  # type: ignore[arg-type]  # collector emits a CITATION_KINDS member
            ref=cs.ref,
            sha=cs.sha,
            verified=cs.matches,
        )
        for cs in collection.citations_status
    ]


def _unconcluded_items(collection: EvidenceCollection) -> list[UnconcludedItem]:
    """Terminal campaigns with no conclusion naming them, each dated (period only).

    Pure identity matching (the collector's ``unconcluded`` reduction); a row
    with no usable completion ts is dropped (``completed_at`` is a required
    dated field — an undated item cannot honestly age).
    """
    items: list[UnconcludedItem] = []
    for a in collection.unconcluded:
        if not isinstance(a.ts, str) or not a.ts:
            continue
        items.append(
            UnconcludedItem(scope_kind="campaign", scope_id=a.subject_id, completed_at=a.ts)
        )
    return items


def _skipped_lines(
    collection: EvidenceCollection, fleet_skipped: list[dict[str, str]]
) -> list[SkippedNamespace]:
    """Union the fleet namespace skips with the collector's intra-namespace gaps.

    Fleet discovery skips (a wiped / torn ``repo.json``) ride verbatim; the
    collector's per-store ``Skipped`` gaps (a corrupt journal line, an
    unaddressable store) are disclosed under a ``"<source>/<subject>"`` ref so a
    single-experiment read still surfaces them (fail-open accounting — nothing
    crashes, everything is counted).
    """
    lines = [SkippedNamespace(ref=entry["ref"], reason=entry["reason"]) for entry in fleet_skipped]
    for s in collection.skipped:
        lines.append(SkippedNamespace(ref=f"{s.source}/{s.subject_id}", reason=s.reason))
    lines.sort(key=lambda s: (s.ref, s.reason))
    return lines


# --- the render seam (late import; stub-friendly) ----------------------------


def _render_period(
    collection: EvidenceCollection, *, since: str, until: str | None, computed_at: str
) -> str:
    """Call the ``ops/evidence_render.py`` render seam, imported LATE.

    The parallel Wave-B render file may not be importable at module-load time,
    and tests stub it by injecting a ``hpc_agent.ops.evidence_render`` module
    into ``sys.modules``. Importing here (never at module top) keeps this verb
    importable on its own and makes the stub seam trivial.
    """
    import importlib

    evidence_render = importlib.import_module("hpc_agent.ops.evidence_render")
    rendered = evidence_render.render_period(
        collection, since=since, until=until, computed_at=computed_at
    )
    return str(rendered)


# --- the primitive ------------------------------------------------------------


@primitive(
    name="evidence-period",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "The evidence PERIOD digest: a time-window projection over the one "
            "evidence collector (conclusions, per-tag activity, determinism "
            "envelopes) that ENDS with the unconcluded-campaigns list — the "
            "standing invitation to close the conclusion loop. since is the "
            "inclusive window start; until the inclusive end (default open/now). "
            "Read-only, no SSH; recomputed on every read (a content-keyed cache "
            "only memoizes — deleting it changes no output). A pure projection: "
            "identity, counting, and verbatim record fields — it never interprets "
            "what a tag means or what a finding says."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=EvidencePeriodSpec,
        schema_ref=SchemaRef(input="evidence_period"),
    ),
    agent_facing=True,
)
def evidence_period(*, experiment_dir: Path, spec: EvidencePeriodSpec) -> EvidencePeriodResult:
    """Project the evidence stores over ``[since, until]`` → :class:`EvidencePeriodResult`.

    Collects through the ONE collector with ``as_of=until`` (the collector owns
    the upper bound), applies the ``since`` lower-bound window filter here, maps
    the windowed collection to the wire result, and renders the timeline +
    unconcluded list via the ``ops/evidence_render.py`` seam. ``spec.fleet``
    widens the collection to every journaled experiment (non-creating discovery,
    torn namespaces skipped + counted). A content-keyed cache memoizes the whole
    projection; ``cache`` discloses hit / miss / disabled.

    Idempotent by construction: no store, no write, no SSH — derived state
    recomputed from the on-disk records on every call, so it can never drift from
    a second source of truth.
    """
    since = spec.since
    until = spec.until
    tags = list(spec.tags)

    # Content cache key: the spec fields + the per-namespace store fingerprint(s).
    spec_key: dict[str, Any] = {
        "verb": "evidence-period",
        "since": since,
        "until": until,
        "tags": sorted(tags),
        "fleet": spec.fleet,
    }
    if spec.fleet:
        experiment_dirs, fleet_skipped = discover_fleet_experiments()
        fingerprint: Any = {
            d.name: evidence_cache.store_fingerprint(d) for d in sorted(experiment_dirs)
        }
    else:
        experiment_dirs = [Path(experiment_dir)]
        fleet_skipped = []
        fingerprint = evidence_cache.store_fingerprint(Path(experiment_dir))

    key = evidence_cache.compute_key(spec_key, fingerprint)
    cache_state, payload = evidence_cache.lookup(key)
    if cache_state == "hit" and payload is not None:
        payload = dict(payload)
        payload["cache"] = "hit"
        return EvidencePeriodResult.model_validate(payload)

    # Recompute live. as_of=until: the collector owns the inclusive upper bound.
    collections = [collect_evidence(d, tags=tags or None, as_of=until) for d in experiment_dirs]
    merged = (
        _merge_collections(collections)
        if collections
        else _merge_collections(
            [collect_evidence(Path(experiment_dir), tags=tags or None, as_of=until)]
        )
    )
    windowed = _window_filter(merged, since=since)

    computed_at = utcnow_iso()
    result = EvidencePeriodResult(
        computed_at=computed_at,
        as_of=until,
        conclusions=_conclusion_lines(windowed),
        activity=_activity_lines(windowed),
        envelopes=_envelope_lines(windowed),
        unconcluded=_unconcluded_items(windowed),
        citations_status=_citation_lines(windowed),
        skipped=_skipped_lines(windowed, fleet_skipped),
        cache=cache_state,
        render=_render_period(windowed, since=since, until=until, computed_at=computed_at),
    )

    evidence_cache.store_result(key, result.model_dump(mode="json"))
    return result
