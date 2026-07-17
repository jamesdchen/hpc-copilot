"""``cite-check`` — the number → paper transcription audit (the last-mile link).

A read-only ``query`` primitive (the ``extract-recipe`` / ``run-story`` / ``trace``
posture): no SSH, no scheduler, no write, no store. Derived state recomputed on
every call. Given a manuscript and exactly ONE sealed seed, it asks per number in
the manuscript — **is this digit faithfully transcribed from the sealed mechanical
chain?** — and buckets it as ``matched`` (equals a sealed value under the
faithful-render tolerance) or ``uncitable`` (no sealed value backs it), offering
``nearest_chain_value`` as pure CONTEXT on the uncitable ones. It DISCLOSES; it
never gates. (``docs/design/cite-check.md`` — the v1 two-bucket shape, Option B;
the ruling-gated label-anchored ``mismatch`` bucket is an additive v2.)

It COMPOSES the shipped machinery — it reinvents nothing:

* the seed → sealed table resolution is ``extract_recipe._resolve_seed`` (the
  ``run_id`` / ``campaign_id`` / ``aggregate_path`` seed contract, reused
  verbatim; a pack ``*.csv`` stays OPAQUE, R2);
* the citing authority is the sealed ``metrics_aggregate.json``'s
  ``aggregated_metrics`` VALUES, flattened by ``verify_relay.collect_source_numbers``
  — read AS SEALED, never re-derived. This is the load-bearing difference from
  ``extract-recipe`` (which is FORBIDDEN from reading those values): **cite-check
  MUST read the values — comparing a cited digit to the sealed digit is its whole
  job.** It still never INTERPRETS a metric (no "best", no metric meaning); it only
  COMPARES a number to a number, an explicitly-permitted core operation (Q1
  substrate-not-semantics);
* the number grammar (``verify_relay.NUM_RE``), the faithful-match tolerance
  (``match_number``), the nearest-value context (``nearest_number``), and the
  false-positive discipline (the ISO-date / month-day / size-suffix / run-id-ident
  / conversational / spelled-cardinal consumers) are the ``verify_relay`` originals
  imported, not copied — PLUS the manuscript-specific reference exclusions this file
  adds (page / figure / table / section / equation refs, citation years,
  bibliography markers, path-embedded digits) and the conservative claim-shape
  filter (prefer decimals / percentages; a bare small integer is low-signal).

NOT MCP-curated: like ``extract-recipe`` / ``trace`` / ``run-story`` it is an
operator/reviewer projection, and the curated catalog is a deliberate
human-amplification allowlist (the MCP-is-projection ruling), so it is reachable
via the CLI registry but kept OUT of ``mcp_server._CURATED_EXTRA_VERBS``.

This file lives at the ``ops/`` *role root* (sibling to ``extract_recipe.py`` /
``trace.py`` / ``run_story.py``) because it reads across subjects — the sealed
aggregate (aggregate subject), the ``extract_recipe`` seed resolver (``ops`` root),
and the ``verify_relay`` extraction discipline (decision subject). The
subject-imports lint short-circuits for role-root files, so the cross-subject reads
here are allowed by construction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.cite_check import CiteCheckInput, CiteCheckResult, CiteFinding
from hpc_agent.cli._dispatch import CliShape, SchemaRef

__all__ = ["cite_check"]


# ── manuscript-specific reference exclusions (NEW — the false-positive soul) ────
# A paper is saturated with dense REFERENCE numbers that are NOT result claims — a
# class ``verify-relay`` never had to exclude because an LLM relay does not write
# them. Each consumer below carves out the shaped offenders BEFORE the number
# pre-pass sees them (the same span-consume discipline verify-relay uses for ISO
# dates). "Which bare decimal is a reported result vs a learning rate" is the
# irreducible Facet-1 judgment the design (docs/design/cite-check.md) bounds with a
# claim-shape filter, disclosed — not with a guessed threshold.

# A reference LABEL immediately followed by a number: page / figure / table /
# section / equation / algorithm / theorem / ... refs. The whole span (label +
# number, incl. an optional dotted sub-number and a range) is consumed, so the ref
# number never reads as a result claim.
_REF_LABEL_NUM_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    r"pp|pgs?|pages?|p"
    r"|figs?|figures?"
    r"|tabs?|tables?"
    r"|secs?|sections?"
    r"|eqn?s?|equations?"
    r"|appendix|appendices|apps?"
    r"|chapters?|chaps?|ch"
    r"|algorithms?|algs?"
    r"|listings?"
    r"|lines?"
    r"|theorems?|thms?|lemmas?|corollary|corollaries"
    r"|definitions?|defs?|propositions?|props?"
    r"|notes?|remarks?|steps?|rows?|columns?|cols?|panels?|items?|parts?"
    r"|refs?|references?"
    r"|versions?|revs?"
    r")\.?\s*\#?\s*\d+(?:\.\d+)*(?:\s*[-–]\s*\d+(?:\.\d+)*)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# An academic CITATION carrying a year — a 4-digit (19xx / 20xx) year is a
# reference, not a result: ``(Smith, 2024)`` / ``(Smith et al., 2024)`` /
# ``[Smith 2024]`` / ``Smith (2024)`` / ``Smith et al. 2024``. The whole span is
# consumed so the year never reads as a claim. Only YEAR-RANGE 4-digit values are
# swept (a genuine 4-digit result of that magnitude is rare and lands, disclosed,
# out of scope — the safe direction for a false-positive-averse audit).
_AUTHOR = r"[A-Z][A-Za-z.'’‐-]+"
_YEAR = r"(?:19|20)\d{2}[a-z]?"
_CITATION_RE = re.compile(
    r"(?:"
    rf"[(\[]\s*{_AUTHOR}(?:\s+(?:et\s+al\.?|and|&|{_AUTHOR}))*,?\s*{_YEAR}\s*[)\]]"
    rf"|{_AUTHOR}(?:\s+et\s+al\.?)?\s*[(\[]\s*{_YEAR}\s*[)\]]"
    rf"|(?<![A-Za-z]){_AUTHOR}(?:\s+et\s+al\.?)?,?\s+{_YEAR}(?![0-9])"
    r")"
)

# A bracketed BIBLIOGRAPHY marker — ``[12]`` / ``[12, 13]`` / ``[12-15]`` — an
# integer-only citation index, never a result. A bracketed DECIMAL (``[0.94]``) is
# NOT a bib marker (the ``\d+`` runs stop at the ``.``), so it is left for the
# number pass.
_BIB_MARKER_RE = re.compile(r"\[\s*\d+(?:\s*[-,–]\s*\d+)*\s*\]")


def _read_json(path: Path) -> Any:
    """Parse a JSON file, or None on any absence/read/parse error (never raises)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _resolve_manuscript(spec: CiteCheckInput) -> str:
    """Resolve the manuscript text from exactly one of text / path.

    Raises :class:`errors.SpecInvalid` when not exactly one manuscript source is
    supplied, or when ``manuscript_path`` names a file that cannot be read.
    """
    has_text = spec.manuscript_text is not None and spec.manuscript_text != ""
    path_str = (spec.manuscript_path or "").strip()
    has_path = bool(path_str)
    if has_text == has_path:
        raise errors.SpecInvalid(
            "cite-check requires exactly one manuscript source: --manuscript-text "
            "XOR --manuscript-path"
        )
    if has_path:
        p = Path(path_str)
        if not p.is_file():
            raise errors.SpecInvalid(
                f"cite-check: manuscript_path {path_str!r} does not exist — there is "
                "no manuscript to audit."
            )
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise errors.SpecInvalid(
                f"cite-check: manuscript_path {path_str!r} could not be read: {exc}"
            ) from exc
    return spec.manuscript_text or ""


def _sealed_pool(
    experiment_dir: Path,
    seed_kind: str,
    seed_ref: str,
    candidates: list[str],
    artifact_opaque: bool,
) -> tuple[set[str], list[float], list[str]]:
    """The sealed citable-value pool — ``(strings, floats, sources_consulted)``.

    The authority is the sealed ``metrics_aggregate.json``'s ``aggregated_metrics``
    VALUES, read AS SEALED and flattened by ``verify_relay.collect_source_numbers``
    (only leaf VALUES enter the pool — the run-id / metric-name KEYS are never
    visited, so a metric NAME is never read for meaning). An OPAQUE pack ``*.csv``
    contributes NOTHING (its content is never parsed, R2) — every manuscript number
    is then uncitable-against-it. A missing aggregate likewise yields an empty pool
    honestly, never a fabricated one.
    """
    from hpc_agent.ops.decision.journal.verify_relay import collect_source_numbers

    strings: set[str] = set()
    floats: list[float] = []
    sources: list[str] = []
    if artifact_opaque:
        return strings, floats, sources

    agg_paths: list[Path] = []
    if seed_kind == "aggregate":
        agg_paths.append(Path(seed_ref))
    elif seed_kind == "run":
        agg_paths.append(experiment_dir / "_aggregated" / seed_ref / "metrics_aggregate.json")
    else:  # campaign — each contributing run's own sealed table
        for rid in candidates:
            agg_paths.append(experiment_dir / "_aggregated" / rid / "metrics_aggregate.json")

    for p in agg_paths:
        data = _read_json(p)
        if not isinstance(data, dict):
            continue
        metrics = data.get("aggregated_metrics")
        if not isinstance(metrics, dict):
            continue
        collect_source_numbers(metrics, strings, floats)
        sources.append(str(p))
    return strings, floats, sources


def _is_high_signal(raw: str) -> bool:
    """A conservative claim-shape filter (the Facet-1 bound, disclosed).

    A decimal, a percentage, a comma-grouped value, or a large bare integer is a
    HIGH-signal citable shape — a non-matching one is disclosed as ``uncitable``.
    A bare SMALL integer (< 1000) is LOW-signal: overwhelmingly a hyperparameter /
    count in prose ("300 epochs", "5 seeds"), not a citable result — a non-matching
    one is skipped (counted, never flagged), so the report is not flooded. (A
    matching low-signal integer is still surfaced as ``matched``, for auditability.)
    """
    from hpc_agent.ops.decision.journal.verify_relay import normalize_num

    if "%" in raw or "," in raw:
        return True
    norm = normalize_num(raw)
    if "." in norm:
        return True
    try:
        return abs(int(norm)) >= 1000
    except ValueError:
        return True


def _in_path(text: str, start: int, end: int) -> bool:
    """True when the number at ``[start, end)`` abuts a path separator (``/`` / ``\\``).

    A digit inside a filesystem path (``results/2024/run.csv``, ``/data/03/``) is
    not a citable result — the manuscript-side analogue of verify-relay's run-id /
    ident consume.
    """
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return before in "/\\" or after in "/\\"


def _extract_findings(
    manuscript: str, strings: set[str], floats: list[float]
) -> tuple[list[CiteFinding], int]:
    """Extract every manuscript number and bucket it — ``(findings, claims_checked)``.

    Reuses the ``verify_relay`` extraction discipline VERBATIM (imported) and layers
    the manuscript-specific reference exclusions on top, then compares each surviving
    number to the sealed pool under ``match_number``.
    """
    from hpc_agent.ops.decision.journal.verify_relay import (
        BARE_MONTH_DAY_RE,
        IDENT_RE,
        ISO_DATETIME_RE,
        NUM_RE,
        SIZE_SUFFIX_RE,
        extract_number_word_claims,
        is_conversational_number,
        is_run_id_like,
        overlaps,
    )

    findings: list[CiteFinding] = []
    claims_checked = 0
    consumed: list[tuple[int, int]] = []

    # (0) dates — verify-relay's ISO / bare month-day consumers (a date is not a
    #     result number). Consume the whole span; audited as neither.
    for m in ISO_DATETIME_RE.finditer(manuscript):
        consumed.append((m.start(), m.end()))
    for m in BARE_MONTH_DAY_RE.finditer(manuscript):
        consumed.append((m.start(), m.end()))

    # (0b) manuscript references — bib markers, academic citations, and labelled
    #      refs (page / figure / table / section / equation / ...). NEW to
    #      cite-check; the false-positive class a manuscript is saturated with.
    for ref_re in (_BIB_MARKER_RE, _CITATION_RE, _REF_LABEL_NUM_RE):
        for m in ref_re.finditer(manuscript):
            consumed.append((m.start(), m.end()))

    # (1) run-id / ident tokens — the digits inside a run-id (``run-3``,
    #     ``pi-train-d363e2a3``) are not claims (verify-relay's ident pre-pass, run
    #     before the number pass so a run-id's embedded digit is consumed first).
    for m in IDENT_RE.finditer(manuscript):
        if is_run_id_like(m.group(0), ""):
            consumed.append((m.start(), m.end()))

    # (2) the numeric-literal pre-pass — the ONE grammar; every maximal span.
    for m in NUM_RE.finditer(manuscript):
        if overlaps(m.start(), m.end(), consumed):
            continue
        raw = m.group(0)
        if is_conversational_number(manuscript, m.start(), m.end(), raw):
            continue  # list marker / ``~2 minutes`` chatter
        size = SIZE_SUFFIX_RE.match(manuscript, m.end())
        if size is not None:
            # A unit-suffixed size ("886M") is a rounded human figure, not a
            # citable count (verify-relay's size-suffix carve-out).
            consumed.append((m.start(), size.end()))
            continue
        if _in_path(manuscript, m.start(), m.end()):
            consumed.append((m.start(), m.end()))
            continue
        consumed.append((m.start(), m.end()))
        _bucket(raw, raw, _is_high_signal(raw), strings, floats, findings)
        claims_checked += 1

    # (2b) spelled-out cardinals >= 13 (verify-relay's number-word claims) — a
    #      restated count is the same transcription; always HIGH-signal (rare in
    #      prose, deliberate).
    for start, end, surface, value in extract_number_word_claims(manuscript):
        if overlaps(start, end, consumed):
            continue
        _bucket(str(value), surface, True, strings, floats, findings)
        claims_checked += 1

    return findings, claims_checked


def _bucket(
    norm: str,
    surface: str,
    high_signal: bool,
    strings: set[str],
    floats: list[float],
    findings: list[CiteFinding],
) -> None:
    """Bucket one number: ``matched`` (equals a sealed value) or ``uncitable``.

    A low-signal non-matching number is skipped (counted by the caller, never
    flagged) — the conservative Facet-1 bound. ``nearest_chain_value`` rides an
    uncitable finding as pure CONTEXT (never an assertion of alignment).
    """
    from hpc_agent.ops.decision.journal.verify_relay import match_number, nearest_number

    if match_number(norm, strings, floats):
        findings.append(
            CiteFinding(
                claim=surface,
                kind="matched",
                detail="cited value equals a sealed chain value (faithful-render tolerance)",
                nearest_chain_value=None,
            )
        )
        return
    if not high_signal:
        return  # low-signal bare small integer — skipped-with-accounting
    findings.append(
        CiteFinding(
            claim=surface,
            kind="uncitable",
            detail="no sealed value backs this digit",
            nearest_chain_value=nearest_number(norm, floats),
        )
    )


@primitive(
    name="cite-check",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Audit a manuscript's numbers against a SEALED reduced table: per number, "
            "is this digit faithfully transcribed from the sealed mechanical chain? "
            "Two buckets — matched (equals a sealed aggregated_metrics value under the "
            "faithful-render tolerance) and uncitable (no sealed value backs it, with "
            "the nearest sealed value offered as CONTEXT). Reuses verify-relay's number "
            "grammar + false-positive discipline (dates, run-ids, page/figure/table/"
            "equation refs, citation years, [12] bibliography markers are NOT claims). "
            "DISCLOSES, never gates. Read-only, no SSH. Manuscript = one of "
            "--manuscript-text / --manuscript-path; seed = exactly one of --run-id / "
            "--campaign-id / --aggregate-path. A pack *.csv is OPAQUE (never parsed)."
        ),
        spec_arg=True,
        spec_model=CiteCheckInput,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="cite_check"),
    ),
    agent_facing=True,
)
def cite_check(experiment_dir: Path, *, spec: CiteCheckInput) -> dict[str, Any]:
    """Return the per-number transcription audit of a manuscript vs. a sealed table.

    Resolves the manuscript + the sealed seed (the ``extract-recipe`` seed
    contract), pools the sealed ``aggregated_metrics`` VALUES as the citing
    authority, extracts every manuscript number under the ``verify-relay``
    discipline (plus the manuscript-specific reference exclusions), and buckets each
    as ``matched`` / ``uncitable`` — offering ``nearest_chain_value`` as CONTEXT on
    the uncitable ones. Pure derived state, recomputed from disk on every call.

    Raises :class:`errors.SpecInvalid` on a bad manuscript source (not exactly one),
    a bad seed (not exactly one), or an absent manuscript / aggregate path.
    """
    from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput
    from hpc_agent.ops.cite_render import render_cite_check
    from hpc_agent.ops.extract_recipe import _apply_exclusions, _resolve_seed

    experiment_dir = Path(experiment_dir)

    manuscript = _resolve_manuscript(spec)
    recipe_input = ExtractRecipeInput(
        run_id=spec.run_id,
        campaign_id=spec.campaign_id,
        aggregate_path=spec.aggregate_path,
    )
    seed_kind, seed_ref, candidates, artifact_opaque, _gaps = _resolve_seed(
        experiment_dir, recipe_input
    )
    # The citing AUTHORITY must be exactly the recipe's KEPT chain, not the raw
    # candidate universe: a campaign seed's candidates include canary /
    # superseded / dead-end runs whose stale _aggregated tables would otherwise
    # let cite-check bless a number that lives ONLY in a run the recipe excludes
    # (provenance-chain review Finding 1). Carve with the same mechanical
    # exclusions extract-recipe applies. No-op for run/aggregate seeds (their
    # pool reads the single seed table, not `candidates`).
    candidates, _excluded = _apply_exclusions(experiment_dir, candidates)

    strings, floats, sources = _sealed_pool(
        experiment_dir, seed_kind, seed_ref, candidates, artifact_opaque
    )
    findings, claims_checked = _extract_findings(manuscript, strings, floats)

    seed_kind_typed: Literal["run", "campaign", "aggregate"] = seed_kind  # type: ignore[assignment]
    result = CiteCheckResult(
        clean=not any(f.kind == "uncitable" for f in findings),
        claims_checked=claims_checked,
        findings=findings,
        sources_consulted=sources,
        seed_kind=seed_kind_typed,
        seed_ref=seed_ref,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    # The markdown render rides on the dumped dict so the render path stays
    # wire-free (the ops op owns the Pydantic boundary).
    dumped["markdown"] = render_cite_check(dumped)
    return dumped
