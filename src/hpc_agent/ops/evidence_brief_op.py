"""``evidence-brief`` — the POINT query over the evidence memory (E5, primary).

A read-only ``verb="query"`` primitive (``docs/design/evidence-memory.md`` Wave B
/ T4-sibling; the ``ops/attention_op.py`` posture: no SSH, no side effects,
``idempotent=True``, ``requires_ssh=False``, MCP-exposed, agent-facing). Given a
key — scope ``tags`` and/or a ``lineage`` run_id — it projects the cross-store
evidence digest for that scope: dated, sha-cited conclusions (newest current
first), per-tag prior-work counts, per-lineage determinism envelopes quoted
VERBATIM from the fingerprint ledger, and each current conclusion's citations
re-resolved at READ (``cited (verified)`` / ``cited (unresolvable here)`` — a
disclosure, NEVER a refusal).

Every surface routes through the ONE collector
(``state/evidence.py::collect_evidence``); this verb adds only the ops-side
composition the state substrate cannot do itself:

* **the dossier resolver injection** — ``state`` never imports ``ops``, so a
  ``dossier`` citation's resolver (``ops/export_dossier.py::
  compute_dossier_signature``) is passed in here (the drift-log item 2 seam).
* **the content-keyed cache** (``state/evidence_cache.py``, the
  ``describe_cache`` posture) — a hit is served only when nothing the collector
  would walk changed; ``cache`` is recorded HONESTLY (``hit``/``miss``/
  ``disabled``) and the index is disposable (deleting it recomputes byte-equal).
* **fleet mode** — the identical per-namespace walk over
  ``ops/attention_queue.py::discover_fleet_experiments`` (non-creating glob
  discovery; a torn namespace is skipped and counted, never fatal).
* **the render seam** — ``ops/evidence_render.py::render_brief`` (T4, built in
  parallel) composes the deterministic markdown that rides the result for
  verbatim relay. Imported late/guarded so this module lands before T4.

This is a DEDICATED query verb: unlike the greenlight embeds (which fail open),
it MAY raise honestly on a structural spec error (a malformed tag slug refuses
via the collector). Collection I/O noise stays tolerant/disclosed throughout.

This file lives at the ``ops/`` role root (sibling to ``attention_op.py`` /
``export_dossier.py``); the subject-imports lint short-circuits for role-root
files, so the cross-subject reads + the ``export_dossier`` composition are
allowed by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.evidence import (
    ActivityLine,
    CitationKind,
    CitationStatusLine,
    ConclusionLine,
    ConclusionStatus,
    EnvelopeLine,
    EvidenceBriefResult,
    EvidenceBriefSpec,
    SkippedNamespace,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops import export_dossier
from hpc_agent.ops.attention_queue import discover_fleet_experiments
from hpc_agent.ops.evidence_project import apply_evidence_order, project_envelope_lines
from hpc_agent.state import evidence_cache
from hpc_agent.state.evidence import (
    CURRENT,
    EvidenceCollection,
    collect_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# --- the render seam (T4, built in parallel) ---------------------------------
# ``ops/evidence_render.py::render_brief(collection, *, computed_at, as_of=None)
# -> str`` is authored by the parallel T4 agent. Import it at MODULE level so a
# test can monkeypatch ``evidence_brief_op._render_brief``, guarded so this
# module imports cleanly before T4 lands. When absent, a deterministic
# placeholder keeps the verb functional (byte-stable for the cache) — the human
# digest is empty-but-honest until the renderer is present.
try:  # pragma: no cover — the seam flips to "present" once T4 lands
    from hpc_agent.ops.evidence_render import render_brief as _render_brief
except ImportError:  # pragma: no cover
    _render_brief = None  # type: ignore[assignment]

__all__ = ["evidence_brief"]


# --- the injected dossier resolver (drift-log item 2 seam) -------------------


def _dossier_resolver_for(experiment_dir: Path) -> Callable[[str], str | None]:
    """A ``ref -> bundle_sha256 | None`` resolver bound to *experiment_dir*.

    ``state/evidence.py`` never imports ``ops``, so the ``dossier`` citation
    resolver is composed HERE and injected into ``collect_evidence``. It routes
    through the ONE signature seam (``compute_dossier_signature`` — the R2
    live-store re-gather). At READ, an unresolvable dossier (a wiped run, an
    archived store) returns ``None`` → the collector DISCLOSES it, never raises
    (only the append gate refuses loudly).
    """

    def _resolve(ref: str) -> str | None:
        try:
            sig = export_dossier.compute_dossier_signature(experiment_dir, ref)
        except Exception:  # noqa: BLE001 — read-side: any failure is "unresolvable here"
            return None
        return sig.bundle_sha256

    return _resolve


# --- collection → wire projection (mechanism-nouned, deterministic) ----------


def _conclusion_lines(coll: EvidenceCollection) -> list[ConclusionLine]:
    """Project the reduced conclusions → the digest's dated, sha-cited lead lines."""
    lines: list[ConclusionLine] = []
    for c in coll.conclusions:
        lines.append(
            ConclusionLine(
                conclusion_id=c.conclusion_id,
                ts=c.ts or "",
                tags=list(c.tags),
                cited_shas=[cit["sha"][:8] for cit in c.citations],
                # CURRENT|REVOKED ⊂ ConclusionStatus (the collector never yields
                # a per-id superseded/absent into ``conclusions``).
                status=cast(ConclusionStatus, c.status),
                finding=c.finding,
            )
        )
    return lines


def _tags_in_scope(coll: EvidenceCollection) -> set[str]:
    """Every tag the activity should report a per-tag row for.

    The union of: the query tags, tags with a scope-journal / look-ledger row,
    and tags a matched run declared (a run tagged in its sidecar but with no
    scope journal still deserves a row — identity + counting, never invention).
    """
    tags: set[str] = set(coll.tags)
    for a in coll.activity:
        if a.kind == "tag":
            tags.add(a.subject_id)
        else:
            for m in a.matched_by:
                if m not in ("all", "lineage") and not m.startswith("retro:"):
                    tags.add(m)
    return tags


def _activity_lines(coll: EvidenceCollection) -> list[ActivityLine]:
    """Fold the heterogeneous activity items into per-tag COUNT rows (E-render).

    Counts and dates only — no ranking, no urgency (the queue's D6 rule). ``looks``
    / ``lineages`` come from the tag's scope row; ``campaigns`` / ``runs`` count
    the matched items carrying the tag; ``newest`` is the newest ts across them.
    """
    tag_items = {a.subject_id: a for a in coll.activity if a.kind == "tag"}
    lines: list[ActivityLine] = []
    for tag in sorted(_tags_in_scope(coll)):
        item = tag_items.get(tag)
        detail: dict[str, Any] = dict(item.detail) if item is not None else {}
        newest = item.ts if item is not None else None
        runs = 0
        campaigns = 0
        for a in coll.activity:
            if tag not in a.matched_by:
                continue
            if a.kind == "run":
                runs += 1
            elif a.kind == "campaign":
                campaigns += 1
            if a.ts and (newest is None or a.ts > newest):
                newest = a.ts
        lines.append(
            ActivityLine(
                tag=tag,
                campaigns=campaigns,
                runs=runs,
                lineages=int(detail.get("distinct_lineages", 0) or 0),
                looks=int(detail.get("prior_looks", 0) or 0),
                newest=newest,
            )
        )
    return lines


def _format_envelope(cls: str, rel_spread: float | None, lo: float | None, hi: float | None) -> str:
    """A short envelope string for the wire result (the human render is T4's).

    Presentation only — the evidence LABELS (n / n_full / …) ride the wire
    verbatim; this composes a compact spread string from the ledger's own
    reduction, never recomputing or reinterpreting a number.
    """
    if rel_spread is not None:
        return f"±{rel_spread * 100:.2f}% rel"
    if lo is not None and hi is not None:
        return f"[{lo}, {hi}]"
    return cls


def _envelope_lines(coll: EvidenceCollection) -> list[EnvelopeLine]:
    """Project each per-key determinism envelope, evidence labels QUOTED VERBATIM.

    Shared loop (``ops/evidence_project.py``); the brief's own 2-decimal
    formatter is injected and stays local.
    """
    return project_envelope_lines(
        coll.envelopes, lambda e: _format_envelope(e.cls, e.rel_spread, e.lo, e.hi)
    )


def _citation_status_lines(coll: EvidenceCollection) -> list[CitationStatusLine]:
    """Project the read-time citation re-resolution disclosure (verified / not)."""
    return [
        CitationStatusLine(
            conclusion_id=cs.conclusion_id,
            kind=cast(CitationKind, cs.kind),  # CITATION_KINDS ⊂ CitationKind
            ref=cs.ref,
            sha=cs.sha,
            verified=cs.resolved and cs.matches,
        )
        for cs in coll.citations_status
    ]


# --- fleet merge (the identical walk, unioned + re-sorted) -------------------


def _merge_collections(
    colls: Sequence[EvidenceCollection],
    *,
    as_of: str | None,
    tags: Sequence[str],
    lineage: str | None,
) -> EvidenceCollection:
    """Union per-namespace collections into ONE, re-applying the collector's order.

    Fleet mode is the SAME per-namespace walk over discovered experiments; the
    union is re-sorted with ``collect_evidence``'s exact total order so the fleet
    digest is as byte-reproducible as a single-namespace one.
    """
    conclusions = [c for coll in colls for c in coll.conclusions]
    activity = [a for coll in colls for a in coll.activity]
    envelopes = [e for coll in colls for e in coll.envelopes]
    unconcluded = [u for coll in colls for u in coll.unconcluded]
    citations_status = [c for coll in colls for c in coll.citations_status]
    skipped = [s for coll in colls for s in coll.skipped]

    apply_evidence_order(
        conclusions=conclusions,
        activity=activity,
        unconcluded=unconcluded,
        envelopes=envelopes,
        citations_status=citations_status,
        skipped=skipped,
    )

    return EvidenceCollection(
        experiment_dir="<fleet>",
        as_of=as_of,
        tags=tuple(tags),
        lineage=lineage,
        conclusions=tuple(conclusions),
        activity=tuple(activity),
        envelopes=tuple(envelopes),
        unconcluded=tuple(unconcluded),
        citations_status=tuple(citations_status),
        skipped=tuple(skipped),
    )


def _render(coll: EvidenceCollection, *, computed_at: str, as_of: str | None) -> str:
    """Render via the T4 seam when present; a deterministic placeholder otherwise."""
    if _render_brief is not None:
        return cast(str, _render_brief(coll, computed_at=computed_at, as_of=as_of))
    return f"evidence · computed {computed_at}"  # T4 render seam absent — byte-stable stub


# --- the primitive ------------------------------------------------------------


@primitive(
    name="evidence-brief",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "The evidence brief: the POINT query over the research program's "
            "sealed records. Keyed by scope tags and/or a lineage run_id, it "
            "projects the cross-store digest for that scope — dated sha-cited "
            "conclusions (newest current first), per-tag prior-work counts, "
            "per-lineage determinism envelopes quoted verbatim from the "
            "fingerprint ledger, and each conclusion's citations re-resolved at "
            "read (verified / unresolvable — disclosed, never refused). Cheap "
            "(journal-first, no SSH), content-cached, fleet-capable. Read-only; "
            "renders a deterministic markdown digest relayed to the human verbatim."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=EvidenceBriefSpec,
        schema_ref=SchemaRef(input="evidence_brief"),
    ),
    agent_facing=True,
)
def evidence_brief(*, experiment_dir: Path, spec: EvidenceBriefSpec) -> EvidenceBriefResult:
    """Project the point-query evidence digest for a scope (tags and/or lineage).

    Single-experiment scope by default; ``spec.fleet`` widens to every experiment
    this machine has journaled (non-creating glob discovery — a torn namespace is
    skipped and counted in ``skipped``). The cache is content-keyed over the walked
    stores' fingerprints and ``cache`` is recorded honestly; deleting the cache
    recomputes a byte-equal result. Citation re-resolution DISCLOSES at read (the
    conclusion stays a truthful dated record); only the append gate refuses.

    Raises :class:`errors.SpecInvalid` on a structural spec error (a malformed
    query-tag slug, via the collector) — this dedicated query verb is honest;
    only the embedded advisory seats fail open.
    """
    now = utcnow_iso()
    exp = Path(experiment_dir)
    tags = list(spec.tags)

    # Content cache key (E-cache): the spec fields + the per-namespace store
    # fingerprint (os.stat only — no reads, non-creating). Any append to any
    # walked store moves an mtime → a new key → recompute.
    spec_key = {"tags": tags, "lineage": spec.lineage, "as_of": spec.as_of, "fleet": spec.fleet}
    if spec.fleet:
        experiments, ns_skipped = discover_fleet_experiments()
        fingerprint: Any = {str(e): evidence_cache.store_fingerprint(e) for e in experiments}
    else:
        experiments = [exp]
        ns_skipped = []
        fingerprint = evidence_cache.store_fingerprint(exp)
    key = evidence_cache.compute_key(spec_key, fingerprint)

    cache_state, payload = evidence_cache.lookup(key)
    if cache_state == "hit" and payload is not None:
        try:
            return EvidenceBriefResult.model_validate(payload).model_copy(update={"cache": "hit"})
        except Exception:  # noqa: BLE001 — a corrupt cached payload → recompute live
            pass

    # Miss / disabled: recompute live. Per-namespace walk through the ONE
    # collector with the injected dossier resolver.
    collections = [
        collect_evidence(
            e,
            tags=spec.tags or None,
            lineage=spec.lineage,
            as_of=spec.as_of,
            dossier_resolver=_dossier_resolver_for(e),
        )
        for e in experiments
    ]
    coll = (
        _merge_collections(collections, as_of=spec.as_of, tags=tags, lineage=spec.lineage)
        if spec.fleet
        else collections[0]
    )

    result = EvidenceBriefResult(
        computed_at=now,
        as_of=spec.as_of,
        conclusions=_conclusion_lines(coll),
        activity=_activity_lines(coll),
        envelopes=_envelope_lines(coll),
        citations_status=_citation_status_lines(coll),
        skipped=[SkippedNamespace(ref=s["ref"], reason=s["reason"]) for s in ns_skipped],
        cache=cache_state,  # "miss" | "disabled" — recorded honestly
        render=_render(coll, computed_at=now, as_of=spec.as_of),
    )

    if cache_state == "miss":
        evidence_cache.store_result(key, result.model_dump())
    return result


# Reference the CURRENT status constant so the "current conclusions lead" contract
# stays a named import (the collector already filters citations_status to current).
_CURRENT = CURRENT
