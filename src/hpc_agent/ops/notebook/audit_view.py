"""The deterministic audit VIEW — the D6 *interface* over an audited ``.py``.

Design: ``docs/design/notebook-audit.md`` (Wave B / T5, governed by **D6**
archive-vs-interface and **D-attention** tiered sign-off). The complete record
(source ``.py`` + decision journal) is the ARCHIVE; this module renders the
INTERFACE — a deterministic, canonical-JSON, per-section projection the human
signs against and whose ``view_sha`` binds *what the human saw* into the
sign-off record (``resolved={audit_id, section, section_sha, view_sha}``).

Per source section the projection carries four things, all mechanical:

* **diff-from-template** — a stdlib :mod:`difflib` unified diff between the
  template's section (matched by slug) and the source's section, over the
  *normalized* source (so ``section_sha`` equality ⇔ empty diff by
  construction). Each section is CLASSIFIED by source-hash (D6):

  * ``inherited`` — the slug exists in the template AND the section shas are
    equal (empty diff);
  * ``added`` — the slug is absent from the template (diffed against nothing);
  * ``modified`` — the slug exists in the template but the shas differ.

* **assertion table** — the ``ast.Assert`` nodes found in the section, as a
  STATIC table (test text + line-within-section + optional message). No
  execution ever happens here; the table records what the code *declares*, not
  what a run *proved*.

* **lint flags** — the caller-passed findings filtered to this section and
  embedded OPAQUELY (never parsed, never interpreted). A finding is attributed
  to a section by a slug-naming key it carries (``slug`` / ``section`` /
  ``section_slug``); a finding with no such key is module-scoped and attributed
  to no section (so it cannot silently flip a section's tier).

* **tier** (D-attention) — ``auto_cleared`` iff ALL THREE of: the section is
  ``inherited`` (empty diff-from-template), it has zero lint flags, AND its
  declared assertions are GREEN. Everything else → ``human_required``.

  The assertion-green leg, stated conservatively (**unverified ≠ green** — the
  choice recorded here per the T5 brief): a section with ZERO declared
  assertions satisfies the leg STATICALLY (there is nothing to prove). A
  section WITH declared assertions is green ONLY when an execution *receipt*
  says so — v1 has no execution by default, so absent a receipt such a section
  is NOT green and reads ``human_required`` (an unrun assertion is not a passed
  assertion). The optional ``receipt`` mapping ``{slug: {output_sha, error}}``
  is accepted opaquely for v1.5 forward-compat: ``error is False`` marks that
  section's assertions green; ``error`` truthy (or a missing/rejecting entry)
  leaves them un-green.

``view_sha`` is sha256 over the CANONICAL JSON of the projection (sorted keys,
compact separators, no timestamps, no absolute paths) so identical inputs yield
an identical sha on every platform. The PER-SECTION ``SectionView.view_sha`` is
the primary object (D5's sign-off record binds a per-section ``view_sha``); the
whole-view ``AuditView.view_sha`` is a deterministic roll-up over the section
shas plus the two module fingerprints, so ANY section edit (or a preamble edit)
moves it.

``render_markdown`` is the code-rendered human projection (the
``ops/relay_render.py`` posture): pure, deterministic formatting of the same
fields — NO LLM-freeform prose enters the audit path (D6).

Pure and stdlib-only (``ast`` / ``difflib`` / ``hashlib`` / ``json``): no
``@primitive`` (the verb wrapper is a later wave), no I/O, no ``_wire`` import,
no dependency on ``ops/notebook/lint.py`` (its findings arrive as an opaque
parameter). Hashing routes through ``state.audit_source``'s one primitive.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hpc_agent.execution.mapreduce.data_trace_contract import (
    RECEIPT_GRADE_SOURCES,
    TRACE_SOURCE_FIELD,
)
from hpc_agent.state.audit_source import ParsedModule, normalize_source
from hpc_agent.state.data_trace import records_sha

__all__ = [
    "INHERITED",
    "ADDED",
    "MODIFIED",
    "AUTO_CLEARED",
    "HUMAN_REQUIRED",
    "Assertion",
    "SectionView",
    "AuditView",
    "build_audit_view",
    "render_markdown",
]

#: Classification of a source section against the template (by source-hash, D6).
INHERITED = "inherited"
ADDED = "added"
MODIFIED = "modified"

#: The two D-attention tiers.
AUTO_CLEARED = "auto_cleared"
HUMAN_REQUIRED = "human_required"

#: The subject_kind an eventual attestation over one of these views carries
#: (T6/T8 build the attestation; T5 only names the constant so the surfaces
#: agree). Opaque to the attestation kernel.
SUBJECT_KIND = "notebook-section"

#: Keys a lint finding may carry to name its section, checked in this order. A
#: finding with none of these is module-scoped (attributed to no section).
_FINDING_SLUG_KEYS = ("slug", "section", "section_slug")


@dataclass(frozen=True)
class Assertion:
    """One statically-discovered ``assert`` in a section.

    * ``test`` — the asserted expression, ``ast.unparse``-d (stable text).
    * ``lineno`` — 1-based line WITHIN the section source (relative, never an
      absolute path — deterministic across checkouts).
    * ``msg`` — the assert's message expression text, or ``None``.
    """

    test: str
    lineno: int
    msg: str | None


@dataclass(frozen=True)
class SectionView:
    """The deterministic projection of ONE source section (the primary object).

    ``view_sha`` is sha256 over :attr:`payload`'s canonical JSON — the exact
    thing a sign-off binds (D5). ``payload`` is the JSON-native dict the sha is
    taken over; it is exposed so a caller can serialize the view verbatim.
    """

    slug: str
    classification: str
    section_sha: str
    template_section_sha: str | None
    diff: tuple[str, ...]
    assertions: tuple[Assertion, ...]
    lint_flags: tuple[Mapping[str, Any], ...]
    tier: str
    view_sha: str
    payload: Mapping[str, Any]
    trace_summary: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AuditView:
    """The whole-source audit view: the per-section projections plus a roll-up.

    * ``sections`` — one :class:`SectionView` per SOURCE section, in source
      order.
    * ``dropped_template_slugs`` — template slugs absent from the source (a
      section the template declared but the draft dropped); surfaced for the
      human, never silently hidden. The graduation gate (T9) is what actually
      refuses on these — the view only shows them.
    * ``view_sha`` — the deterministic roll-up sha over the section shas and the
      two module fingerprints; any section OR preamble edit moves it.
    * ``payload`` — the JSON-native module-level dict ``view_sha`` is taken over.
    """

    sections: tuple[SectionView, ...]
    dropped_template_slugs: tuple[str, ...]
    source_module_sha: str
    template_module_sha: str
    view_sha: str
    payload: Mapping[str, Any]
    #: The compose-ready DRAFT for each dropped template slug (draft-at-pass):
    #: ``(slug, template_section_source)`` in template order. Pure presentation —
    #: NOT part of ``view_sha`` (the roll-up already covers the dropped slugs), and
    #: never applied to the source. Defaulted for back-compat with any positional
    #: constructor; :func:`build_audit_view` always populates it.
    dropped_template_drafts: tuple[tuple[str, str], ...] = ()


# ── canonical JSON / hashing ─────────────────────────────────────────────────


def _canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, compact separators, unicode kept as-is.

    The one serialization every ``view_sha`` is taken over — deterministic and
    platform-stable (no timestamps, no absolute paths ever enter the payloads).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha_json(obj: Any) -> str:
    """sha256 hexdigest of :func:`_canonical_json` of *obj* (utf-8)."""
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def _plainify(obj: Any) -> Any:
    """Coerce an opaque finding into JSON-native structures for canonical JSON.

    Mappings → sorted-key dicts, sequences → lists, scalars untouched. Keeps a
    lint finding embeddable in the canonical payload WITHOUT interpreting it
    (str/bytes stay leaves, never treated as sequences).
    """
    if isinstance(obj, Mapping):
        return {str(k): _plainify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plainify(v) for v in obj]
    return obj


# ── per-section projection pieces ────────────────────────────────────────────


def _finding_slug(finding: Mapping[str, Any]) -> str | None:
    """The section slug a lint *finding* names, or ``None`` (module-scoped)."""
    for key in _FINDING_SLUG_KEYS:
        value = finding.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _flags_for(slug: str, findings: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """The findings attributed to *slug*, in caller order, embedded opaquely."""
    return tuple(f for f in findings if _finding_slug(f) == slug)


def _classify(source_sha: str, template_sha: str | None) -> str:
    """Classify a section by source-hash (D6): inherited / added / modified."""
    if template_sha is None:
        return ADDED
    return INHERITED if source_sha == template_sha else MODIFIED


def _diff_from_template(source_src: str, template_src: str, slug: str) -> tuple[str, ...]:
    """A stdlib unified diff of NORMALIZED template → source for *slug*.

    Diffing the normalized source makes ``section_sha`` equality ⇔ empty diff by
    construction (both derive from the same normalization). Fixed file labels
    and ``lineterm=""`` keep the output free of timestamps and platform newline
    quirks — fully deterministic.
    """
    template_lines = normalize_source(template_src).split("\n")
    source_lines = normalize_source(source_src).split("\n")
    return tuple(
        difflib.unified_diff(
            template_lines,
            source_lines,
            fromfile=f"template:{slug}",
            tofile=f"source:{slug}",
            lineterm="",
        )
    )


def _assertions(section_src: str) -> tuple[Assertion, ...]:
    """The ``ast.Assert`` nodes in *section_src* as a static table.

    Tolerant of a mid-draft :class:`SyntaxError` (returns an empty table rather
    than raising — the view is a projection, not a compiler; the lint owns
    structural refusal). ``lineno`` is relative to the section source.
    """
    try:
        tree = ast.parse(section_src)
    except SyntaxError:
        return ()
    found: list[Assertion] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            found.append(
                Assertion(
                    test=ast.unparse(node.test),
                    lineno=node.lineno,
                    msg=ast.unparse(node.msg) if node.msg is not None else None,
                )
            )
    found.sort(key=lambda a: (a.lineno, a.test))
    return tuple(found)


def _assertions_green(
    assertions: tuple[Assertion, ...],
    slug: str,
    receipt: Mapping[str, Any] | None,
    current_section_sha: str,
) -> bool:
    """Whether a section's declared assertions count as GREEN (the tier leg).

    Conservative — unverified ≠ green (T5 brief): zero assertions is green
    STATICALLY (nothing to prove); with assertions, green requires a receipt
    entry ``{slug: {..., error: False}}``. Absent a receipt (v1 default) a
    section with assertions is NOT green.

    Sha-freshness (T10): a receipt entry greens a section only when its
    ``error`` is ``False`` AND — when the entry carries a ``section_sha`` (a
    JOURNALED receipt, produced by :func:`~hpc_agent.state.notebook_audit.read_render_receipts`)
    — that sha equals the section's CURRENT sha. A journaled receipt for an
    older sha is drift and greens nothing. An entry WITHOUT a ``section_sha`` is
    an INLINE preview receipt (the read-only ``notebook-audit-view`` path, which
    journals nothing) and keeps v1 behavior: ``error is False`` alone greens it.
    The mutate path (``notebook-auto-clear``) only ever feeds journaled,
    sha-bearing entries, so it cannot be greened by a drifted receipt.
    """
    if not assertions:
        return True
    if receipt is None:
        return False
    entry = receipt.get(slug)
    if not isinstance(entry, Mapping) or entry.get("error") is not False:
        return False
    recorded_sha = entry.get("section_sha")
    if recorded_sha is not None:
        return bool(recorded_sha == current_section_sha)
    return True


def _tier(classification: str, flags_count: int, assertions_green: bool) -> str:
    """The D-attention tier: auto_cleared iff inherited ∧ no flags ∧ green."""
    if classification == INHERITED and flags_count == 0 and assertions_green:
        return AUTO_CLEARED
    return HUMAN_REQUIRED


# ── the section join — per-section runtime-evidence summary (Amendment 16) ────
#
# B3-LEAN: each human_required section's trusted render carries a per-section
# summary — one line per OBSERVABLE whose value CHANGED across the section
# (first→last), the LATEST execution only, citing the SET-sha of the section's
# record subset. Freshness rides ``section_sha`` the runner stamps: a section
# whose latest-execution records are NOT stamped with the current section_sha is
# STALE and its summary is ELIDED with a disclosed marker (a missing stamp =
# stale, so a runner that does not stamp degrades honestly, never rendered as if
# current). Receipts/sign-off surfaces consume RUNNER-TIER records ONLY (A10).

_RECEIPT_GRADE = frozenset(RECEIPT_GRADE_SOURCES)


def _latest_execution(runner_records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The LAST execution's records, segmenting the runner stream on a ``seq`` reset.

    One ``observe_source`` pass emits ``seq`` monotone from 0 across ALL its
    sections; a new pass restarts at 0. So a record whose ``seq`` does not exceed
    the prior one opens a new execution — the last segment is the latest execution
    (Amendment 16 "the LATEST execution only"). Records lacking an int ``seq`` do
    not open a segment (they ride the current one). Pure.
    """
    execs: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    prev_seq: int | None = None
    for rec in runner_records:
        seq = rec.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            if prev_seq is not None and seq <= prev_seq:
                execs.append(current)
                current = []
            prev_seq = seq
        current.append(rec)
    if current:
        execs.append(current)
    return execs[-1] if execs else []


def _changed_observables(subset: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """First→last atoms per observable that MOVED across the section (else nothing).

    Groups the section's records by ``stage`` (the observable name), keeping the
    FIRST and LAST atoms seen in append order; an observable whose first and last
    atoms are equal did not move and contributes NO line (Amendment 16 "unchanged
    observables render nothing"). Observable order is first-appearance order for a
    deterministic render.
    """
    first: dict[str, Any] = {}
    last: dict[str, Any] = {}
    order: list[str] = []
    for rec in subset:
        name = rec.get("stage")
        if not isinstance(name, str) or not name:
            continue
        atoms = rec.get("atoms")
        if name not in first:
            first[name] = atoms
            order.append(name)
        last[name] = atoms
    return [
        {"observable": name, "first": first[name], "last": last[name]}
        for name in order
        if first[name] != last[name]
    ]


def _section_trace_summary(
    slug: str, current_section_sha: str, audit_traces: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """The B3-LEAN per-section runtime summary, or ``None`` when nothing to show.

    Reduces the audit-scope trace records to this section's evidence:

    1. keep RUNNER-TIER records only (A10 — sign-off never sees engine/draft);
    2. take the LATEST execution (:func:`_latest_execution`) then this section's
       subset (records tagged with *slug*);
    3. no subset → ``None`` (no evidence; the section renders as it always did);
    4. FRESHNESS — the subset's stamped ``section_sha`` set must be exactly
       ``{current_section_sha}``; any missing/mismatched stamp → STALE, returns
       ``{"stale": True}`` (the summary is ELIDED with a disclosed marker, never
       rendered as if current);
    5. the CHANGED observables (:func:`_changed_observables`); none changed →
       ``None`` (unchanged renders nothing);
    6. else ``{"stale": False, "section_records_sha": <SET-sha of the subset>,
       "changed": [...]}``.

    Pure. The returned mapping is JSON-native (it enters the hashed payload, so it
    is part of what the human signs).
    """
    runner = [r for r in audit_traces if r.get(TRACE_SOURCE_FIELD) in _RECEIPT_GRADE]
    subset = [r for r in _latest_execution(runner) if r.get("section") == slug]
    if not subset:
        return None
    stamped = {r.get("section_sha") for r in subset}
    if stamped != {current_section_sha}:
        return {"stale": True}
    changed = _changed_observables(subset)
    if not changed:
        return None
    return {
        "stale": False,
        "section_records_sha": records_sha([dict(r) for r in subset]),
        "changed": changed,
    }


# ── the builder ──────────────────────────────────────────────────────────────


def build_audit_view(
    source: ParsedModule,
    template: ParsedModule,
    lint_findings: Sequence[Mapping[str, Any]],
    *,
    receipt: Mapping[str, Any] | None = None,
    attention_order: Sequence[str] | None = None,
    audit_traces: Sequence[Mapping[str, Any]] | None = None,
) -> AuditView:
    """Build the deterministic :class:`AuditView` for *source* against *template*.

    Projects each SOURCE section (classification, diff-from-template, assertion
    table, opaque lint flags, tier) and rolls the per-section ``view_sha`` shas
    into a whole-view ``view_sha``. *lint_findings* is consumed OPAQUELY (a
    sequence of finding mappings, each optionally naming its section slug);
    *receipt* is the opaque execution receipt (``{slug: {output_sha, error,
    section_sha?}}``) — an entry greens a section's assertions per
    :func:`_assertions_green` (a journaled entry carrying ``section_sha`` greens
    only while fresh; an inline entry greens on ``error is False`` alone). Pure —
    same inputs yield the same view and shas on every platform.

    *attention_order* (T12) is a caller-supplied slug ordering applied to the
    presented sections (and thus the markdown). The DEFAULT (``None``) is source
    order — deterministic, no reordering. When given, listed slugs are emitted
    FIRST in the given order; unknown slugs (not in the source) are ignored;
    slugs the source has but the order omits keep source order AFTER the listed
    ones. Because it changes what the human is shown, the resulting order feeds
    the module-level ``view_sha`` (a reorder that actually moves sections moves
    the roll-up); per-section ``view_sha`` values are unaffected. It is pure
    caller config — never a tier or authorship input.

    *audit_traces* (A16 B3-LEAN, the section join) are the audit-scope trace
    records (``read_trace(experiment_dir, "audit", audit_id, 0)``; ``None`` = no
    trace). Each ``human_required`` section gets a per-section runtime summary
    (:func:`_section_trace_summary`): one line per observable whose value moved
    across the section's LATEST execution, citing the SET-sha of the section's
    record subset. It is bound into that section's ``view_sha`` (signed evidence,
    not presentation) and is ABSENT when there is nothing to show — byte-identical
    to a trace-free view. A STALE section (its latest records not stamped with the
    current ``section_sha``, or unstamped) is elided with a disclosed marker.
    """
    template_by_slug = {s.slug: s for s in template.sections}
    traces = audit_traces or ()

    section_views: list[SectionView] = []
    for sect in source.sections:
        tmpl = template_by_slug.get(sect.slug)
        template_sha = tmpl.section_sha if tmpl is not None else None
        classification = _classify(sect.section_sha, template_sha)
        diff = _diff_from_template(sect.source, tmpl.source if tmpl is not None else "", sect.slug)
        assertions = _assertions(sect.source)
        flags = _flags_for(sect.slug, lint_findings)
        green = _assertions_green(assertions, sect.slug, receipt, sect.section_sha)
        tier = _tier(classification, len(flags), green)

        # Section join (A16 B3-LEAN): runtime evidence rides ONLY the sections
        # that route human attention, and enters the hashed payload (signed
        # evidence). Absent when there is nothing to show → byte-identical.
        summary = (
            _section_trace_summary(sect.slug, sect.section_sha, traces)
            if tier == HUMAN_REQUIRED
            else None
        )

        payload: dict[str, Any] = {
            "slug": sect.slug,
            "classification": classification,
            "section_sha": sect.section_sha,
            "template_section_sha": template_sha,
            "diff": list(diff),
            "assertions": [{"test": a.test, "lineno": a.lineno, "msg": a.msg} for a in assertions],
            "lint_flags": [_plainify(f) for f in flags],
            "tier": tier,
        }
        if summary is not None:
            payload["trace_summary"] = summary
        section_views.append(
            SectionView(
                slug=sect.slug,
                classification=classification,
                section_sha=sect.section_sha,
                template_section_sha=template_sha,
                diff=diff,
                assertions=assertions,
                lint_flags=flags,
                tier=tier,
                view_sha=_sha_json(payload),
                payload=payload,
                trace_summary=summary,
            )
        )

    # T12: apply the caller-supplied attention ordering to the PRESENTED
    # sections. Stable sort — listed slugs sort by their position; every unlisted
    # slug shares the sentinel key and so keeps source order after the listed
    # ones. Unknown slugs in the order simply never match a section. Reordering
    # here (before the module payload) is what threads attention_order into
    # view_sha and the markdown.
    if attention_order is not None:
        position = {slug: i for i, slug in enumerate(attention_order)}
        sentinel = len(position)
        section_views.sort(key=lambda sv: position.get(sv.slug, sentinel))

    source_slugs = set(source.slugs)
    dropped = tuple(s.slug for s in template.sections if s.slug not in source_slugs)
    # Draft-at-pass: the missing section's draft is KNOWN — it is the template's
    # own cell source verbatim. Compose it (never applied to the source), disclosed
    # only in the markdown footer, so it never enters the ``view_sha`` payload.
    dropped_drafts = tuple(
        (s.slug, s.source) for s in template.sections if s.slug not in source_slugs
    )

    module_payload: dict[str, Any] = {
        "source_module_sha": source.module_sha,
        "template_module_sha": template.module_sha,
        "dropped_template_slugs": list(dropped),
        "sections": [{"slug": sv.slug, "view_sha": sv.view_sha} for sv in section_views],
    }
    return AuditView(
        sections=tuple(section_views),
        dropped_template_slugs=dropped,
        source_module_sha=source.module_sha,
        template_module_sha=template.module_sha,
        view_sha=_sha_json(module_payload),
        payload=module_payload,
        dropped_template_drafts=dropped_drafts,
    )


# ── the code-rendered markdown projection ────────────────────────────────────


def _render_section(sv: SectionView) -> list[str]:
    """The markdown lines for one section (pure, deterministic)."""
    lines: list[str] = []
    lines.append(f"## section: {sv.slug}  [tier: {sv.tier}]")
    lines.append("")
    lines.append(f"- classification: {sv.classification}")
    lines.append(f"- section_sha: {sv.section_sha}")
    lines.append(f"- view_sha: {sv.view_sha}")
    lines.append("")

    lines.append("### diff-from-template")
    lines.append("")
    if sv.diff:
        lines.append("```diff")
        lines.extend(sv.diff)
        lines.append("```")
    else:
        lines.append("(no changes — inherited from template)")
    lines.append("")

    lines.append("### assertions")
    lines.append("")
    if sv.assertions:
        for a in sv.assertions:
            suffix = f"  ({a.msg})" if a.msg is not None else ""
            lines.append(f"- L{a.lineno}: {a.test}{suffix}")
    else:
        lines.append("(none declared)")
    lines.append("")

    lines.append("### lint flags")
    lines.append("")
    if sv.lint_flags:
        for flag in sv.lint_flags:
            lines.append(f"- {_canonical_json(_plainify(flag))}")
    else:
        lines.append("(none)")
    lines.append("")

    lines.extend(_render_trace_summary(sv))
    return lines


def _render_trace_summary(sv: SectionView) -> list[str]:
    """The section-join runtime-evidence block (A16 B3-LEAN), or nothing.

    Rendered ONLY when a section carries a ``trace_summary`` (a human_required
    section with runtime evidence): a STALE section discloses the elision and
    shows no values (never rendered as if current); a fresh section lists one
    line per CHANGED observable (first→last atoms) and cites the SET-sha of the
    section's record subset. Deterministic; carries no verdict vocabulary — the
    values are the runner's own measurements.
    """
    summary = sv.trace_summary
    if not summary:
        return []
    lines = ["### runtime evidence (latest execution)", ""]
    if summary.get("stale"):
        lines.append(
            "(section trace is STALE — its latest execution predates the current "
            "section code; the summary is elided)"
        )
        lines.append("")
        return lines
    lines.append(f"- section_records_sha: {summary.get('section_records_sha')}")
    for entry in summary.get("changed", ()):
        first = _canonical_json(_plainify(entry.get("first")))
        last = _canonical_json(_plainify(entry.get("last")))
        lines.append(f"- {entry.get('observable')}: {first} -> {last}")
    lines.append("")
    return lines


def render_markdown(view: AuditView) -> str:
    """Render *view* as deterministic, code-authored markdown (D6 posture).

    Pure formatting of the projection's own fields — slug, tier, classification,
    diff, assertions, flags per section — with NO LLM-freeform prose. Same view
    → byte-identical markdown.
    """
    lines: list[str] = []
    lines.append("# Notebook audit view")
    lines.append("")
    lines.append(f"- view_sha: {view.view_sha}")
    lines.append(f"- source module_sha: {view.source_module_sha}")
    lines.append(f"- template module_sha: {view.template_module_sha}")
    if view.dropped_template_slugs:
        dropped = ", ".join(view.dropped_template_slugs)
        lines.append(
            f"- dropped template sections (present in template, absent in source): {dropped}"
        )
    lines.append("")

    if not view.sections:
        lines.append("(no sections)")
        lines.append("")
    for sv in view.sections:
        lines.extend(_render_section(sv))

    lines.extend(_render_next_actions(view))
    lines.extend(_render_dropped_drafts(view))

    return "\n".join(lines).rstrip() + "\n"


def render_summary_markdown(view: AuditView) -> str:
    """The bodies-OMITTED render (run-#12 finding 12; user-ruled: OMIT at the
    source, never compact downstream).

    Under popup-primary the model is no longer the display channel: the diff /
    assertion / flag BODIES live in the per-section render files and the
    sign-off popup, so shipping ~11k tokens of them through the agent every
    loop pass is pure cost plus a re-summarization temptation. This render
    carries the header, ONE metadata line per section (slug, tier,
    classification, sha12s, counts), and the SAME next-actions footer +
    compose-ready dropped drafts. Nothing here summarizes a body — it is the
    metadata BESIDE the bodies, deterministic and code-authored like its
    sibling.
    """
    lines: list[str] = []
    lines.append("# Notebook audit view (metadata; bodies live in the render files + popup)")
    lines.append("")
    lines.append(f"- view_sha: {view.view_sha}")
    lines.append(f"- source module_sha: {view.source_module_sha}")
    lines.append(f"- template module_sha: {view.template_module_sha}")
    if view.dropped_template_slugs:
        dropped = ", ".join(view.dropped_template_slugs)
        lines.append(
            f"- dropped template sections (present in template, absent in source): {dropped}"
        )
    lines.append("")
    if not view.sections:
        lines.append("(no sections)")
        lines.append("")
    for sv in view.sections:
        added = sum(1 for ln in sv.diff if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in sv.diff if ln.startswith("-") and not ln.startswith("---"))
        lines.append(
            f"- {sv.slug}  [{sv.tier}] {sv.classification} — "
            f"section_sha {sv.section_sha[:12]}, view_sha {sv.view_sha[:12]}, "
            f"diff +{added}/-{removed}, {len(sv.assertions)} assertion(s), "
            f"{len(sv.lint_flags)} lint flag(s)"
        )
    lines.append("")
    lines.extend(_render_next_actions(view))
    lines.extend(_render_dropped_drafts(view))
    return "
".join(lines).rstrip() + "
"


def _render_dropped_drafts(view: AuditView) -> list[str]:
    """Compose-ready drafts for the sections the source DROPPED (draft-at-pass).

    When the source omits a template section, the missing section's draft is
    KNOWN — it is the template's own cell source, verbatim (marker included). The
    poka-yoke composes it here so the human/LLM pastes a structurally-complete
    section, rather than being merely TOLD a slug is missing and left to re-derive
    it (the run-#10 conversion doctrine: compose what code can). Pure presentation:
    it is NOT part of ``view_sha`` (the roll-up already covers the dropped SLUGS,
    exactly as ``_render_next_actions`` adds nothing the sha covers), and it NEVER
    edits the source — applying the draft stays the human's/LLM's act.
    """
    if not view.dropped_template_drafts:
        return []
    lines: list[str] = ["## compose the dropped sections", ""]
    lines.append(
        "These template sections are absent from the source; the graduation gate "
        "(T9) refuses on them. Each draft below is the template's own cell source "
        "(marker included) — paste it into the source in template order, then "
        "re-run the loop from lint. Applying it is your act; the view never edits "
        "the source."
    )
    lines.append("")
    for slug, source in view.dropped_template_drafts:
        lines.append(f"### {slug}")
        lines.append("")
        lines.append("```python")
        lines.append(source.rstrip("\n"))
        lines.append("```")
        lines.append("")
    return lines


def _render_next_actions(view: AuditView) -> list[str]:
    """The copy-ready next-actions footer (run-#10 amendment: hyper-palatable
    sign-off — the human's next keystroke is visible in the artifact itself).

    Derived ONLY from the view's own tiers, so it changes nothing the
    ``view_sha`` covers (the sha rolls from per-section shas; this is pure
    presentation) and nothing the per-section trusted renders contain. The
    stated bar is the GATE'S actual bar (token-exact slug naming) — rendered
    by code precisely because a relaying model overstated it live in run #10.
    """
    pending = [sv.slug for sv in view.sections if sv.tier == HUMAN_REQUIRED]
    lines: list[str] = ["## next actions", ""]
    if not pending:
        lines.append(
            "(no sections await sign-off — every section is auto_cleared; "
            "notebook-status reports the audit verdict)"
        )
        lines.append("")
        return lines
    lines.append("Sections awaiting a typed human sign-off:")
    for sv in view.sections:
        if sv.tier == HUMAN_REQUIRED:
            lines.append(f"- {sv.slug}  (view_sha {sv.view_sha[:12]})")
    lines.append("")
    batch = " ".join(pending)
    lines.append(f'- To sign: type an utterance naming each slug, e.g. "sign {batch}"')
    lines.append(
        "- To contest one: name it with what is wrong, e.g. "
        f'"{pending[0]}: <what is wrong>" — a nudge re-enters the loop at lint'
    )
    lines.append(
        "- The gate's bar: the utterance must NAME each signed slug token-exactly; "
        "a bare ack is refused. (Redundant sign-offs on auto_cleared sections "
        "carry a higher bar.)"
    )
    lines.append("")
    return lines
