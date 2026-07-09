"""T4 tests — ``ops/evidence_render.py``: the two pure digest renders (E-render).

Toy WIDGET vocabulary only (never harxhar/quant — the domain-packs toy-fixture
rule). Fixtures are built as T1 ``state/evidence.py`` dataclasses directly (the
renderer is pure over an :class:`EvidenceCollection`; no store I/O here). Covers:
golden brief + period renders; byte-stability under input-order shuffling;
disclosed truncation; the unconcluded list TERMINATES the period render; the
no-interpretation-vocabulary source-scan pin; and the honest empty render.
"""

from __future__ import annotations

import ast

import pytest

from hpc_agent.ops import evidence_render
from hpc_agent.state import evidence

# --- fixture builders (T1 dataclasses, widget vocabulary) --------------------


def _conc(
    cid: str,
    *,
    ts: str,
    status: str = evidence.CURRENT,
    tags: tuple[str, ...] = (),
    finding: str = "",
    citations: tuple[dict[str, str], ...] = (),
    superseded_count: int = 0,
    concludes: tuple[dict[str, str], ...] = (),
) -> evidence.ConclusionEvidence:
    return evidence.ConclusionEvidence(
        conclusion_id=cid,
        status=status,
        ts=ts,
        tags=tags,
        concludes=concludes,
        citations=citations,
        finding=finding,
        content_sha="c" * 64,
        superseded_count=superseded_count,
        matched_by=("all",),
    )


def _act(kind: str, subject_id: str, *, ts: str, **detail: object) -> evidence.ActivityItem:
    return evidence.ActivityItem(
        kind=kind, subject_id=subject_id, ts=ts, detail=detail, matched_by=("all",)
    )


def _env(cmd_sha: str, key: str, **over: object) -> evidence.EnvelopeEvidence:
    base: dict = {
        "cmd_sha": cmd_sha,
        "key": key,
        "cls": "stochastic",
        "lo": 1.0,
        "hi": 1.02,
        "rel_spread": 0.021,
        "n": 4,
        "n_full": 3,
        "n_partial": 1,
        "scales": ("main",),
        "clusters": ("hoffman2",),
        "same_submission_only": False,
    }
    base.update(over)
    return evidence.EnvelopeEvidence(**base)  # type: ignore[arg-type]


def _cite_status(
    cid: str, kind: str, ref: str, sha: str, *, resolved: bool, matches: bool
) -> evidence.CitationStatus:
    return evidence.CitationStatus(
        conclusion_id=cid,
        kind=kind,
        ref=ref,
        sha=sha,
        resolved=resolved,
        matches=matches,
        detail="",
    )


def _collection(
    *,
    tags: tuple[str, ...] = ("widget",),
    lineage: str | None = None,
    as_of: str | None = None,
    conclusions: tuple[evidence.ConclusionEvidence, ...] = (),
    activity: tuple[evidence.ActivityItem, ...] = (),
    envelopes: tuple[evidence.EnvelopeEvidence, ...] = (),
    unconcluded: tuple[evidence.ActivityItem, ...] = (),
    citations_status: tuple[evidence.CitationStatus, ...] = (),
    skipped: tuple[evidence.Skipped, ...] = (),
) -> evidence.EvidenceCollection:
    return evidence.EvidenceCollection(
        experiment_dir="/toy/widget",
        as_of=as_of,
        tags=tags,
        lineage=lineage,
        conclusions=conclusions,
        activity=activity,
        envelopes=envelopes,
        unconcluded=unconcluded,
        citations_status=citations_status,
        skipped=skipped,
    )


_CITE = {"kind": "run", "ref": "widget-run-1", "sha": "a3f2c9d1beef0000"}


def _rich_collection() -> evidence.EvidenceCollection:
    return _collection(
        tags=("edge-x", "rv-data"),
        as_of="2025-11-14T00:00:00Z",
        conclusions=(
            _conc(
                "edge-x-2025h1",
                ts="2025-11-14T00:00:00Z",
                tags=("edge-x", "rv-data"),
                finding="no widget alpha vs rv-data",
                citations=(_CITE,),
                superseded_count=1,
            ),
        ),
        activity=(
            _act(
                "campaign",
                "widget-camp-1",
                ts="2025-11-02T00:00:00Z",
                terminal=True,
                concluded=True,
            ),
            _act(
                "run",
                "widget-run-1",
                ts="2025-10-30T00:00:00Z",
                cmd_sha="7be4abcd",
                tags=["edge-x"],
            ),
            _act("run", "widget-run-2", ts="2025-10-29T00:00:00Z", cmd_sha="deadbeef", tags=[]),
            _act(
                "tag", "rv-holdout", ts="2025-11-01T00:00:00Z", prior_looks=9, distinct_lineages=2
            ),
        ),
        envelopes=(_env("7be4abcd0000", "widget_error"),),
        citations_status=(
            _cite_status(
                "edge-x-2025h1",
                "run",
                "widget-run-1",
                "a3f2c9d1beef0000",
                resolved=True,
                matches=True,
            ),
        ),
    )


# --- golden brief render -----------------------------------------------------


def test_render_brief_golden() -> None:
    out = evidence_render.render_brief(_rich_collection(), computed_at="2026-07-07T06:12Z")
    expected = (
        "evidence · tags: edge-x, rv-data · computed 2026-07-07T06:12Z"
        " · as_of=2025-11-14T00:00:00Z\n"
        "CONCLUSION 2025-11-14T00:00:00Z · edge-x-2025h1 · cited a3f2c9d1beef (verified)"
        " — no widget alpha vs rv-data\n"
        "  supersedes 1 earlier · tags: edge-x, rv-data\n"
        "PRIOR WORK · 1 campaign(s), 2 run(s), 2 lineage(s) · newest 2025-11-02T00:00:00Z"
        " · 9 look(s) on rv-holdout\n"
        "ENVELOPE · lineage 7be4abcd… · widget_error · stochastic · ±2.1% rel"
        " (n=4: 3 full + 1 partial, scales: main, clusters: hoffman2)\n"
        "UNTAGGED · 1 lineage(s) matched by cmd_sha only (no tags declared — disclosed)\n"
    )
    assert out == expected


def test_render_brief_unresolvable_citation_disclosed() -> None:
    coll = _collection(
        conclusions=(
            _conc(
                "widget-c1",
                ts="2025-06-01T00:00:00Z",
                tags=("widget",),
                finding="held",
                citations=({"kind": "dossier", "ref": "d/w", "sha": "bundlesha0000"},),
            ),
        ),
        citations_status=(
            _cite_status(
                "widget-c1", "dossier", "d/w", "bundlesha0000", resolved=False, matches=False
            ),
        ),
    )
    out = evidence_render.render_brief(coll, computed_at="2026-07-07T00:00Z")
    assert "cited bundlesha000 (unresolvable here)" in out


# --- golden period render + the unconcluded list TERMINATES -------------------


def test_render_period_golden_and_unconcluded_terminates() -> None:
    coll = _collection(
        tags=("widget",),
        conclusions=(
            _conc(
                "widget-c1",
                ts="2025-06-01T00:00:00Z",
                tags=("widget",),
                finding="no alpha",
                citations=(_CITE,),
            ),
        ),
        activity=(
            _act("campaign", "widget-camp-1", ts="2025-05-01T00:00:00Z", terminal=True),
            _act("tag", "widget", ts="2025-04-01T00:00:00Z", prior_looks=3, distinct_lineages=1),
        ),
        unconcluded=(_act("campaign", "widget-camp-2", ts="2025-05-15T00:00:00Z", terminal=True),),
        citations_status=(
            _cite_status(
                "widget-c1", "run", "widget-run-1", "a3f2c9d1beef0000", resolved=True, matches=True
            ),
        ),
    )
    out = evidence_render.render_period(
        coll,
        since="2025-01-01T00:00:00Z",
        until="2025-12-31T00:00:00Z",
        computed_at="2026-07-07T06:12Z",
    )
    lines = out.rstrip("\n").split("\n")
    assert lines[0] == (
        "evidence period · tags: widget · computed 2026-07-07T06:12Z"
        " · 2025-01-01T00:00:00Z → 2025-12-31T00:00:00Z"
    )
    # newest-first timeline
    assert lines[1].startswith("2025-06-01T00:00:00Z · CONCLUSION widget-c1")
    assert lines[2].startswith("2025-05-01T00:00:00Z · CAMPAIGN COMPLETE widget-camp-1")
    assert lines[3].startswith("2025-04-01T00:00:00Z · LOOKS widget")
    # the unconcluded list TERMINATES the render
    assert lines[-2] == "UNCONCLUDED CAMPAIGNS"
    assert lines[-1] == "  2025-05-15T00:00:00Z · widget-camp-2"


def test_render_period_window_excludes_out_of_range() -> None:
    coll = _collection(
        conclusions=(
            _conc("widget-c1", ts="2024-01-01T00:00:00Z", finding="old", citations=(_CITE,)),
        ),
    )
    out = evidence_render.render_period(
        coll, since="2025-01-01T00:00:00Z", until=None, computed_at="2026-07-07T00:00Z"
    )
    assert "widget-c1" not in out
    assert "TIMELINE · no dated activity in window" in out


# --- byte-stability under input-order shuffling ------------------------------


def test_render_brief_byte_stable_under_shuffle() -> None:
    coll = _rich_collection()
    # Re-order the tuples the renderer iterates; a pure render over a fixed
    # collection must never re-derive order from iteration order — but the
    # collector already imposes a total order, so a deliberately shuffled input
    # renders identically ONLY where the renderer re-sorts (period) or aggregates
    # order-insensitively (brief counts). The brief must be byte-identical.
    shuffled = evidence.EvidenceCollection(
        experiment_dir=coll.experiment_dir,
        as_of=coll.as_of,
        tags=coll.tags,
        lineage=coll.lineage,
        conclusions=coll.conclusions,
        activity=tuple(reversed(coll.activity)),
        envelopes=coll.envelopes,
        unconcluded=coll.unconcluded,
        citations_status=coll.citations_status,
        skipped=coll.skipped,
    )
    a = evidence_render.render_brief(coll, computed_at="t")
    b = evidence_render.render_brief(shuffled, computed_at="t")
    assert a == b


def test_render_period_byte_stable_under_shuffle() -> None:
    coll = _collection(
        activity=(
            _act("campaign", "widget-camp-1", ts="2025-05-01T00:00:00Z", terminal=True),
            _act("run", "widget-run-1", ts="2025-04-01T00:00:00Z", cmd_sha="aa"),
            _act("run", "widget-run-2", ts="2025-03-01T00:00:00Z", cmd_sha="bb"),
        ),
    )
    shuffled = evidence.EvidenceCollection(
        experiment_dir=coll.experiment_dir,
        as_of=coll.as_of,
        tags=coll.tags,
        lineage=coll.lineage,
        conclusions=coll.conclusions,
        activity=tuple(reversed(coll.activity)),
        envelopes=coll.envelopes,
        unconcluded=coll.unconcluded,
        citations_status=coll.citations_status,
        skipped=coll.skipped,
    )
    a = evidence_render.render_period(coll, since="2025-01-01T00:00:00Z", computed_at="t")
    b = evidence_render.render_period(shuffled, since="2025-01-01T00:00:00Z", computed_at="t")
    assert a == b


# --- disclosed truncation fires ----------------------------------------------


def test_brief_conclusion_truncation_disclosed() -> None:
    conclusions = tuple(
        _conc(f"widget-c{i}", ts=f"2025-{i:02d}-01T00:00:00Z", finding=f"f{i}", citations=(_CITE,))
        for i in range(1, 8)  # 7 > _MAX_CONCLUSIONS (3)
    )
    out = evidence_render.render_brief(_collection(conclusions=conclusions), computed_at="t")
    assert "+4 more current conclusion(s)" in out
    # exactly the cap is shown as lead CONCLUSION lines
    assert out.count("CONCLUSION 2025-") == evidence_render._MAX_CONCLUSIONS


def test_brief_envelope_truncation_disclosed() -> None:
    n_env = 9
    envelopes = tuple(_env(f"{i:016d}", "widget_error") for i in range(n_env))
    out = evidence_render.render_brief(_collection(envelopes=envelopes), computed_at="t")
    dropped = n_env - evidence_render._MAX_ENVELOPES
    assert f"+{dropped} more lineage envelope(s)" in out


def test_period_timeline_truncation_disclosed() -> None:
    activity = tuple(
        _act("run", f"widget-run-{i}", ts=f"2025-01-{i:02d}T00:00:00Z", cmd_sha=f"s{i}")
        for i in range(1, evidence_render._MAX_TIMELINE + 5)
    )
    out = evidence_render.render_period(
        _collection(activity=activity), since="2025-01-01T00:00:00Z", computed_at="t"
    )
    assert "older dated line(s) omitted" in out


# --- honest empty render (all gaps disclosed) --------------------------------


def test_render_brief_empty_discloses_all_gaps() -> None:
    out = evidence_render.render_brief(_collection(tags=()), computed_at="t")
    assert "tags: (none)" in out
    assert "as_of=(none)" in out
    assert "CONCLUSIONS · none recorded" in out
    assert "PRIOR WORK · none recorded" in out
    assert "ENVELOPE · none recorded" in out


def test_render_period_empty_discloses_all_gaps() -> None:
    out = evidence_render.render_period(
        _collection(tags=()), since="2025-01-01T00:00:00Z", computed_at="t"
    )
    assert "TIMELINE · no dated activity in window" in out
    assert "ENVELOPE · none recorded" in out
    assert "UNCONCLUDED CAMPAIGNS · none" in out


# --- the no-interpretation-vocabulary pin (AST scan over module literals) ----


def test_no_interpretation_vocabulary_in_source() -> None:
    """No urgency/recommendation/interpretation word survives in any string literal.

    The D6 rule mechanized: the render is composed ONLY from record fields. A
    banned word entering a literal is the fabricated-urgency class landing in the
    one surface designed to hold only counts, dates, and shas.
    """
    import pathlib

    src = pathlib.Path(evidence_render.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {
        "urgent",
        "recommend",
        "recommended",
        "recommendation",
        "should",
        "stale",
        "promising",
        "must",
        "warning",
        "danger",
        "critical",
        "advise",
        "suggest",
    }
    # Docstrings are documentation, not rendered output — the module/function
    # docstrings legitimately NAME the banned vocabulary to explain the rule. The
    # pin scans only literals that can reach a digest line, so collect the
    # docstring nodes (first Expr-Constant of every module/def/class body) and
    # exclude them.
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            low = node.value.lower()
            for word in banned:
                if word in low:
                    offenders.append(f"{word!r} in {node.value!r}")
    assert not offenders, offenders


def test_render_brief_returns_str() -> None:
    with pytest.raises(TypeError):
        evidence_render.render_brief(_collection())  # type: ignore[call-arg]  # computed_at required
