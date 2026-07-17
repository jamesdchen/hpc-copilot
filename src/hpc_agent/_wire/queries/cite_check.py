"""Pydantic models for the ``cite-check`` query verb.

``cite-check`` is the number → paper transcription audit (the last-mile link of
the clean-reproduction-extraction program, ``docs/design/cite-check.md``). Given a
manuscript (prose / a ``.tex`` / a ``.md``) and exactly ONE sealed seed (a
``run_id`` / ``campaign_id`` / ``aggregate_path`` — the ``extract-recipe`` seed
contract), it asks per number in the manuscript: **is this digit faithfully
transcribed from the sealed mechanical chain?** The citing authority is the
sealed ``metrics_aggregate.json``'s ``aggregated_metrics`` VALUES, read AS SEALED
(never re-derived, never interpreted — only compared under a transcription
tolerance).

v1 is the TWO-BUCKET shape (``docs/design/cite-check.md`` § Options + recommendation,
Option B): every extracted claim is either ``matched`` (the cited digit equals a
sealed value under the faithful-render tolerance) or ``uncitable`` (no sealed
value backs the digit), with ``nearest_chain_value`` offered as pure CONTEXT on an
uncitable finding (the ``verify-relay`` ``nearest_source_value`` precedent —
offered, never asserted as alignment). The ``mismatch`` bucket (label-anchored
cell alignment) is a ruling-gated, additive v2 refinement and is NOT emitted here.

Boundary posture (the :mod:`hpc_agent._wire.queries.extract_recipe` posture —
flat, no domain vocabulary in field names): cite-check COMPARES a cited number to
a sealed number for transcription fidelity. It never NAMES a metric, never picks a
"best" run, never concludes. It DISCLOSES; it never gates. Field names are drawn
from the substrate vocabulary (claim / kind / detail / nearest_chain_value), never
from domain semantics (metric / accuracy / loss / baseline / ...).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CiteFinding(BaseModel):
    """One extracted manuscript number, bucketed against the sealed value pool.

    ``kind``:

    * ``matched`` — the cited number EQUALS a sealed chain value under the
      faithful-render tolerance (exact / float-equality / pure-truncation-prefix /
      display round-or-truncate — the ``verify_relay.match_number`` semantics).
      This digit in the paper IS the sealed table's digit. Reported for
      auditability; ``clean`` ignores it. ``nearest_chain_value`` is ``None``.
    * ``uncitable`` — the cited number matches no sealed value: "no chain value
      backs this digit." The honest default — cite-check does NOT guess whether it
      is a transcription typo or an incidental prose number. ``nearest_chain_value``
      carries the closest sealed value as CONTEXT (offered, never asserted as an
      alignment to a specific cell).
    """

    model_config = ConfigDict(extra="forbid", title="cite-check finding")

    claim: str = Field(description="The number as it appears in the manuscript (its surface form).")
    kind: Literal["matched", "uncitable"] = Field(
        description="matched (equals a sealed value) or uncitable (no sealed value backs it).",
    )
    detail: str = Field(description="A one-line disclosure of why the number bucketed as it did.")
    nearest_chain_value: str | None = Field(
        default=None,
        description=(
            "On an uncitable finding, the closest sealed value as CONTEXT (never an "
            "assertion that the cited number was meant to be it). None on a matched finding."
        ),
    )


class CiteCheckInput(BaseModel):
    """Inputs to ``cite-check`` — a manuscript + exactly one sealed seed.

    Exactly one of ``manuscript_text`` / ``manuscript_path`` supplies the text
    whose numeric claims are audited; exactly one of ``run_id`` / ``campaign_id`` /
    ``aggregate_path`` names the SEALED artifact whose ``aggregated_metrics`` values
    are the citing authority (the ``extract-recipe`` seed contract, reused verbatim).
    A pack ``*.csv`` seed is accepted only as an OPAQUE citation whose content is
    NEVER parsed (the dossier no-parse boundary) — every manuscript number is then
    uncitable-against-it.
    """

    model_config = ConfigDict(extra="forbid", title="cite-check input spec")

    manuscript_text: str | None = Field(
        default=None,
        description=(
            "The manuscript prose / table verbatim whose numeric claims are audited. "
            "Excludes manuscript_path."
        ),
    )
    manuscript_path: str | None = Field(
        default=None,
        description=(
            "Path to a .tex / .md / .txt manuscript, read tolerantly. Excludes manuscript_text."
        ),
    )
    run_id: str | None = Field(
        default=None,
        description=(
            "Cite against this run's sealed table "
            "(_aggregated/<run_id>/metrics_aggregate.json aggregated_metrics values). "
            "Excludes the other two seeds."
        ),
    )
    campaign_id: str | None = Field(
        default=None,
        description=(
            "Cite against this campaign's sealed tables (each contributing run's "
            "aggregated_metrics values). Excludes the other two seeds."
        ),
    )
    aggregate_path: str | None = Field(
        default=None,
        description=(
            "Path to a sealed reduced-metrics artifact. A metrics_aggregate.json is "
            "read for its aggregated_metrics values; a pack *.csv is an OPAQUE "
            "citation (never parsed). Excludes the other two seeds."
        ),
    )


class CiteCheckResult(BaseModel):
    """The per-number transcription audit — two-bucket disclosure (v1).

    ``clean`` is False iff any ``uncitable`` finding was surfaced (a ``matched``
    finding never affects ``clean``). ``claims_checked`` counts every extracted
    numeric claim that was evaluated (reference numbers — page / figure / table /
    equation / section refs, citation years, bibliography markers — and low-signal
    bare small integers are filtered out before the count, the false-positive
    guard). ``sources_consulted`` honestly lists only the sealed artifacts actually
    read (an absent / opaque artifact yields the empty list, not a fabricated one).
    """

    model_config = ConfigDict(extra="forbid", title="cite-check output data")

    clean: bool = Field(description="False iff any uncitable finding was surfaced.")
    claims_checked: int = Field(
        ge=0,
        description="Count of extracted numeric claims evaluated (references / low-signal filtered).",
    )
    findings: list[CiteFinding] = Field(
        default_factory=list,
        description="One entry per evaluated claim that bucketed as matched or uncitable.",
    )
    sources_consulted: list[str] = Field(
        default_factory=list,
        description="The sealed metrics_aggregate.json artifacts whose values were pooled.",
    )
    seed_kind: Literal["run", "campaign", "aggregate"] = Field(
        description="Which seed reference the sealed value pool was resolved from.",
    )
    seed_ref: str = Field(description="The seed's identity / path verbatim.")
    markdown: str = Field(
        default="",
        description="The code-rendered audit (deterministic; LLM-free render path).",
    )
