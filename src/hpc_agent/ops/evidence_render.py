"""Evidence-memory digests — the two code-rendered projections (E-render).

Design: ``docs/design/evidence-memory.md`` (Wave B, T4). Two pure renderers over
T1's :class:`~hpc_agent.state.evidence.EvidenceCollection`:

* :func:`render_brief` — the POINT digest sized for greenlight embedding: a header
  line, the newest current conclusion per subject (sha-cited, its citation
  re-resolution DISCLOSED), a prior-work counts line, per-lineage envelope
  one-liners quoting the fingerprint ledger's evidence labels VERBATIM, the
  untagged/lineage-only disclosure, and the skipped accounting. Older conclusions
  collapse to a disclosed count (deterministic truncation, no silent caps).
* :func:`render_period` — the WINDOW timeline: dated one-liners (conclusions,
  campaign completions, look activity, run/fingerprint lineage) sorted newest
  first, the per-lineage envelopes, ENDING with the unconcluded-campaigns list —
  each item dated by its completion ts, the place the conclusion loop closes.

Both are PURE string work — no I/O, no journal reads, no ``_wire`` import (the
:mod:`hpc_agent.ops.relay_render` / :mod:`hpc_agent.ops.story_render` posture).
The caller hands in an already-collected :class:`EvidenceCollection` and receives
the digest string. Same collection → byte-identical output (the collector already
imposes a deterministic total order; this module never re-sorts by anything the
collection did not, and never introduces nondeterminism).

**No interpretation, no urgency, no recommendation.** Every string literal here is
composed from the records' OWN fields — counts, dates, shas, verbatim envelope
labels. "3 campaigns" is a count; a sentence telling the reader what to DO is one
core never writes (the attention-queue D6 rule; a source-scan test pins the
absence of urgency/recommendation vocabulary in this module's literals).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hpc_agent.state.evidence import (
        ActivityItem,
        CitationStatus,
        ConclusionEvidence,
        EnvelopeEvidence,
        EvidenceCollection,
        Skipped,
    )

__all__ = ["render_brief", "render_period"]

# The reduced-status literals mirrored from ``state/evidence.py`` (kept as local
# constants so this pure renderer takes no runtime import of the state module —
# the ``story_render`` TYPE_CHECKING-only posture). A drift here is caught by the
# T4 golden tests, which import the real constants.
_CURRENT = "current"
_REVOKED = "revoked"

#: Deterministic, DISCLOSED truncation caps (no silent caps: whatever is dropped
#: is counted and named on a trailing disclosure line).
_MAX_CONCLUSIONS = 3
_MAX_ENVELOPES = 6
_MAX_SKIPPED = 5
_MAX_TIMELINE = 40

#: How many leading hex chars a cited/lineage sha is shown by (the digest quotes a
#: PREFIX; the full sha rides the wire result for exact relay).
_SHA_PREFIX = 12
_LINEAGE_PREFIX = 8


# ── shared vocabulary ─────────────────────────────────────────────────────────


def _tags_phrase(tags: tuple[str, ...]) -> str:
    return ", ".join(tags) if tags else "(none)"


def _sha_prefix(sha: str | None, width: int) -> str:
    return sha[:width] if isinstance(sha, str) and sha else "(none)"


def _members_phrase(members: tuple[str, ...]) -> str:
    return ", ".join(members) if members else "(none)"


def _header_line(kind: str, collection: EvidenceCollection, *, computed_at: str, tail: str) -> str:
    """The ``<kind> · tags: … · lineage … · computed … · <tail>`` header."""
    segs = [kind, f"tags: {_tags_phrase(collection.tags)}"]
    if collection.lineage:
        segs.append(f"lineage {collection.lineage}")
    segs.append(f"computed {computed_at}")
    segs.append(tail)
    return " · ".join(segs)


def _citation_index(
    statuses: Iterable[CitationStatus],
) -> dict[tuple[str, str, str, str], tuple[bool, bool]]:
    """Index re-resolution by ``(conclusion_id, kind, ref, sha)`` → ``(resolved, matches)``."""
    return {(s.conclusion_id, s.kind, s.ref, s.sha): (s.resolved, s.matches) for s in statuses}


def _verify_phrase(resolved_matches: tuple[bool, bool] | None) -> str:
    """Render the citation re-resolution as ``verified`` / ``unresolvable here``.

    ``verified`` iff the evidence was FOUND on this namespace AND the asserted sha
    matched; every other outcome (absent, moved, mismatched) DISCLOSES as
    ``unresolvable here`` — the read-side drift disclosure, never a refusal.
    """
    if resolved_matches is not None and resolved_matches[0] and resolved_matches[1]:
        return "verified"
    return "unresolvable here"


def _lead_citation(conc: ConclusionEvidence) -> dict[str, str] | None:
    return conc.citations[0] if conc.citations else None


def _envelope_line(env: EnvelopeEvidence) -> str:
    """One per-lineage envelope one-liner — evidence labels QUOTED VERBATIM.

    ``±X.X% rel`` renders the record's OWN ``rel_spread`` (a fraction, shown as a
    percent — a unit rendering of the stored number, never a recomputation); the
    ``n / n_full / n_partial / scales / clusters`` block is copied verbatim from
    the ledger's reduction (never paraphrased). ``sha``-cited by the lineage
    ``cmd_sha``.
    """
    spread = "rel n/a" if env.rel_spread is None else f"±{env.rel_spread * 100:.1f}% rel"
    line = (
        f"ENVELOPE · lineage {_sha_prefix(env.cmd_sha, _LINEAGE_PREFIX)}… · {env.key} · "
        f"{env.cls} · {spread} "
        f"(n={env.n}: {env.n_full} full + {env.n_partial} partial, "
        f"scales: {_members_phrase(env.scales)}, clusters: {_members_phrase(env.clusters)})"
    )
    if env.same_submission_only:
        line += " · same-submission only"
    return line


def _envelope_lines(envelopes: tuple[EnvelopeEvidence, ...]) -> list[str]:
    if not envelopes:
        return ["ENVELOPE · none recorded"]
    shown = envelopes[:_MAX_ENVELOPES]
    lines = [_envelope_line(e) for e in shown]
    dropped = len(envelopes) - len(shown)
    if dropped:
        lines.append(f"ENVELOPE · +{dropped} more lineage envelope(s) — run evidence-period")
    return lines


def _skipped_lines(skipped: tuple[Skipped, ...]) -> list[str]:
    """Disclose the collection gaps — corrupt lines / unaddressable stores."""
    if not skipped:
        return []
    shown = skipped[:_MAX_SKIPPED]
    parts = [f"{s.source}:{s.subject_id} ({s.reason})" for s in shown]
    line = f"SKIPPED · {len(skipped)} disclosed gap(s): " + "; ".join(parts)
    dropped = len(skipped) - len(shown)
    if dropped:
        line += f"; +{dropped} more"
    return [line]


def _untagged_lineages(activity: tuple[ActivityItem, ...]) -> int:
    """Distinct ``cmd_sha`` among matched runs that declared NO tags (disclosed)."""
    shas: set[str] = set()
    for a in activity:
        if a.kind != "run":
            continue
        tags = a.detail.get("tags")
        if isinstance(tags, list) and tags:
            continue
        cmd_sha = a.detail.get("cmd_sha")
        if isinstance(cmd_sha, str) and cmd_sha:
            shas.add(cmd_sha)
    return len(shas)


# ── the POINT brief (E-render, sized for embedding) ───────────────────────────


def _conclusion_lines(
    conclusions: tuple[ConclusionEvidence, ...],
    cite_index: dict[tuple[str, str, str, str], tuple[bool, bool]],
) -> list[str]:
    """The lead: the newest current conclusion per subject, sha-cited + disclosed.

    Shows at most :data:`_MAX_CONCLUSIONS`; the rest collapse to a disclosed
    count. Each shown conclusion discloses its ``supersedes N earlier`` count and
    its tags on an indented follow line.
    """
    if not conclusions:
        return ["CONCLUSIONS · none recorded"]

    lines: list[str] = []
    for conc in conclusions[:_MAX_CONCLUSIONS]:
        ts = conc.ts or "(undated)"
        if conc.status == _REVOKED:
            lines.append(f"CONCLUSION {ts} · {conc.conclusion_id} · revoked")
            continue
        lead = _lead_citation(conc)
        if lead is not None:
            key = (conc.conclusion_id, lead["kind"], lead["ref"], lead["sha"])
            cited = (
                f"cited {_sha_prefix(lead['sha'], _SHA_PREFIX)} "
                f"({_verify_phrase(cite_index.get(key))})"
            )
            extra = len(conc.citations) - 1
            if extra > 0:
                cited += f" +{extra} more cited"
        else:
            cited = "cited (none)"
        lines.append(f"CONCLUSION {ts} · {conc.conclusion_id} · {cited} — {conc.finding}")
        follow: list[str] = []
        if conc.superseded_count:
            follow.append(f"supersedes {conc.superseded_count} earlier")
        if conc.tags:
            follow.append(f"tags: {_tags_phrase(conc.tags)}")
        if follow:
            lines.append("  " + " · ".join(follow))

    dropped = len(conclusions) - _MAX_CONCLUSIONS
    if dropped > 0:
        lines.append(
            f"CONCLUSION · +{dropped} more current conclusion(s) — run evidence-brief "
            "for the full list"
        )
    return lines


def _prior_work_line(activity: tuple[ActivityItem, ...]) -> str:
    """The prior-work COUNTS line — campaigns / runs / lineages / newest / looks."""
    campaigns = sum(1 for a in activity if a.kind == "campaign")
    runs = sum(1 for a in activity if a.kind == "run")
    lineages = {
        a.detail["cmd_sha"]
        for a in activity
        if a.kind == "run" and isinstance(a.detail.get("cmd_sha"), str) and a.detail["cmd_sha"]
    }
    if not activity:
        return "PRIOR WORK · none recorded"
    newest = max((a.ts for a in activity if a.ts), default=None)
    segs = [
        f"{campaigns} campaign(s)",
        f"{runs} run(s)",
        f"{len(lineages)} lineage(s)",
    ]
    line = "PRIOR WORK · " + ", ".join(segs)
    if newest:
        line += f" · newest {newest}"
    look_segs = [
        f"{a.detail['prior_looks']} look(s) on {a.subject_id}"
        for a in activity
        if a.kind == "tag"
        and isinstance(a.detail.get("prior_looks"), int)
        and a.detail["prior_looks"]
    ]
    if look_segs:
        line += " · " + ", ".join(look_segs)
    return line


def render_brief(
    collection: EvidenceCollection, *, computed_at: str, as_of: str | None = None
) -> str:
    """Render the POINT digest — the greenlight-embeddable brief (E-render).

    *computed_at* is the render timestamp (echoed in the header); *as_of* echoes
    the collector's inclusive time cut (falls back to the collection's own
    ``as_of``). The digest leads with the newest current conclusion per subject,
    then the prior-work counts, per-lineage envelopes, the untagged/lineage-only
    disclosure, and the skipped accounting. Deterministic and DISCLOSED
    throughout: whatever a cap drops is counted and named, never silently elided.
    """
    effective_as_of = as_of if as_of is not None else collection.as_of
    tail = f"as_of={effective_as_of}" if effective_as_of is not None else "as_of=(none)"
    cite_index = _citation_index(collection.citations_status)

    lines: list[str] = [_header_line("evidence", collection, computed_at=computed_at, tail=tail)]
    lines.extend(_conclusion_lines(collection.conclusions, cite_index))
    lines.append(_prior_work_line(collection.activity))
    lines.extend(_envelope_lines(collection.envelopes))

    untagged = _untagged_lineages(collection.activity)
    if untagged:
        lines.append(
            f"UNTAGGED · {untagged} lineage(s) matched by cmd_sha only "
            "(no tags declared — disclosed)"
        )
    lines.extend(_skipped_lines(collection.skipped))
    return "\n".join(lines) + "\n"


# ── the PERIOD digest (the window timeline + the loop-closing list) ───────────


def _in_window(ts: str | None, since: str, until: str | None) -> bool:
    if not isinstance(ts, str) or not ts:
        return False
    if ts < since:
        return False
    return until is None or ts <= until


def _timeline_rows(
    collection: EvidenceCollection,
    cite_index: dict[tuple[str, str, str, str], tuple[bool, bool]],
    *,
    since: str,
    until: str | None,
) -> list[tuple[str, str]]:
    """The dated one-liners in the window → ``(ts, line)`` rows (unsorted).

    Conclusions, terminal campaign completions, per-tag look activity, and
    run/fingerprint lineage rows — each carrying its own record ``ts``. Rows with
    no usable ``ts`` (which cannot be placed in the window) are excluded — the
    everything-time-indexed discipline.
    """
    rows: list[tuple[str, str]] = []

    for conc in collection.conclusions:
        if not _in_window(conc.ts, since, until):
            continue
        assert conc.ts is not None
        if conc.status == _REVOKED:
            rows.append((conc.ts, f"CONCLUSION {conc.conclusion_id} · revoked"))
            continue
        lead = _lead_citation(conc)
        if lead is not None:
            key = (conc.conclusion_id, lead["kind"], lead["ref"], lead["sha"])
            cited = (
                f"cited {_sha_prefix(lead['sha'], _SHA_PREFIX)} "
                f"({_verify_phrase(cite_index.get(key))})"
            )
        else:
            cited = "cited (none)"
        rows.append((conc.ts, f"CONCLUSION {conc.conclusion_id} · {cited} — {conc.finding}"))

    for a in collection.activity:
        if not _in_window(a.ts, since, until):
            continue
        assert a.ts is not None
        if a.kind == "campaign" and a.detail.get("terminal"):
            rows.append((a.ts, f"CAMPAIGN COMPLETE {a.subject_id}"))
        elif a.kind == "tag":
            looks = a.detail.get("prior_looks")
            lineages = a.detail.get("distinct_lineages")
            rows.append((a.ts, f"LOOKS {a.subject_id} · {looks} look(s), {lineages} lineage(s)"))
        elif a.kind == "run":
            cmd_sha = a.detail.get("cmd_sha")
            rows.append(
                (a.ts, f"RUN {a.subject_id} · cmd_sha {_sha_prefix(cmd_sha, _LINEAGE_PREFIX)}…")
            )

    return rows


def render_period(
    collection: EvidenceCollection,
    *,
    since: str,
    until: str | None = None,
    computed_at: str,
) -> str:
    """Render the WINDOW digest — the timeline, then the unconcluded list (E-render).

    *since* / *until* bound the display window (inclusive; open when *until* is
    ``None``); *computed_at* is the render timestamp. The body is the dated
    timeline (conclusions, campaign completions, look activity, run/fingerprint
    lineage — newest first), then the per-lineage envelopes, and it ENDS with the
    unconcluded-campaigns list — every terminal campaign no current conclusion
    names, each dated by its completion ts (the standing place the conclusion loop
    closes). Same collection + window → byte-identical output.
    """
    window = f"{since} → {until if until is not None else '(open)'}"
    cite_index = _citation_index(collection.citations_status)

    lines: list[str] = [
        _header_line("evidence period", collection, computed_at=computed_at, tail=window)
    ]

    rows = _timeline_rows(collection, cite_index, since=since, until=until)
    # Deterministic order: newest ts first, ties broken by the (stable) line text.
    rows.sort(key=lambda r: r[1])
    rows.sort(key=lambda r: r[0], reverse=True)
    if rows:
        shown = rows[:_MAX_TIMELINE]
        for ts, line in shown:
            lines.append(f"{ts} · {line}")
        dropped = len(rows) - len(shown)
        if dropped:
            lines.append(f"TIMELINE · +{dropped} older dated line(s) omitted")
    else:
        lines.append("TIMELINE · no dated activity in window")

    lines.extend(_envelope_lines(collection.envelopes))
    lines.extend(_skipped_lines(collection.skipped))

    # The loop-closing list ENDS the digest — each item dated by completion ts.
    if collection.unconcluded:
        lines.append("UNCONCLUDED CAMPAIGNS")
        for u in collection.unconcluded:
            ts = u.ts or "(undated)"
            lines.append(f"  {ts} · {u.subject_id}")
    else:
        lines.append("UNCONCLUDED CAMPAIGNS · none")

    return "\n".join(lines) + "\n"
