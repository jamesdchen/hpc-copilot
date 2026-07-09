"""Wire-model tests for the ``evidence-brief`` / ``evidence-period`` queries (T2).

Covers the contracts the T2 wire owes (the full contract-suite mirror is T11):
round-trips over the full shapes, the at-least-one-key refusal on the point
spec (both empty refused; each key alone OK), unknown-field refusal
(``extra="forbid"`` everywhere), the ``_FORBIDDEN_FIELD_NAMES`` walk over every
model's schema (mirrored locally from ``tests/contracts/test_dossier_boundary.py``),
and the opaque ``finding`` round-trip (arbitrary prose survives byte-for-byte).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from hpc_agent._wire.queries.evidence import (
    ActivityLine,
    CitationStatusLine,
    ConclusionLine,
    EnvelopeLine,
    EvidenceBriefResult,
    EvidenceBriefSpec,
    EvidencePeriodResult,
    EvidencePeriodSpec,
    SkippedNamespace,
    UnconcludedItem,
)

# Domain-semantics vocabulary core must never name on the wire — mirrored inline
# from tests/contracts/test_dossier_boundary.py (drift surfaces here; the full
# mirror lands in T11).
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "control",
        "controls",
        "unit",
        "units",
        "metric",
        "metrics",
        "holdout",
        "treatment",
        "baseline",
        "significance",
        "placebo",
        "anchor",
    }
)

_ALL_MODELS: list[type[BaseModel]] = [
    EvidenceBriefSpec,
    EvidencePeriodSpec,
    EvidenceBriefResult,
    EvidencePeriodResult,
    ConclusionLine,
    ActivityLine,
    EnvelopeLine,
    CitationStatusLine,
    UnconcludedItem,
    SkippedNamespace,
]


def _schema_property_names(schema: dict[str, Any]) -> set[str]:
    """Every property NAME anywhere in a JSON schema, recursively (names only)."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(k for k in props if isinstance(k, str))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return names


# --- (a) round-trips over the full shapes -----------------------------------


def test_brief_spec_round_trips_by_tags() -> None:
    spec = EvidenceBriefSpec(tags=["edge-x", "rv-data"])
    assert EvidenceBriefSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_brief_spec_round_trips_by_lineage() -> None:
    spec = EvidenceBriefSpec(lineage="toy-run-abc123", as_of="2025-07-01T00:00:00Z", fleet=True)
    assert EvidenceBriefSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_period_spec_round_trips_full_shape() -> None:
    spec = EvidencePeriodSpec(
        since="2025-01-01T00:00:00Z",
        until="2025-07-01T00:00:00Z",
        tags=["edge-x"],
        fleet=True,
    )
    assert EvidencePeriodSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_period_spec_round_trips_open_ended() -> None:
    spec = EvidencePeriodSpec(since="2025-01-01T00:00:00Z")
    assert spec.until is None
    assert spec.tags == []
    assert EvidencePeriodSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_brief_result_round_trips_full_shape() -> None:
    result = EvidenceBriefResult(
        computed_at="2026-07-08T06:12:00Z",
        as_of="2025-11-14T00:00:00Z",
        conclusions=[
            ConclusionLine(
                conclusion_id="edge-x-2025h1",
                ts="2025-11-14T00:00:00Z",
                tags=["edge-x", "rv-data"],
                cited_shas=["a3f2c9d1"],
                status="current",
                finding="edge-x showed no alpha vs RV data in 2025H1 — see dossier a3f2.",
            )
        ],
        activity=[
            ActivityLine(
                tag="rv-data",
                campaigns=3,
                runs=14,
                lineages=2,
                looks=9,
                newest="2025-11-02T00:00:00Z",
            )
        ],
        envelopes=[
            EnvelopeLine(
                lineage="7be4c1d0",
                envelope="±2.1% rel",
                n=4,
                n_full=3,
                n_partial=1,
                scales=["main"],
                clusters=["hoffman2"],
            )
        ],
        citations_status=[
            CitationStatusLine(
                conclusion_id="edge-x-2025h1",
                kind="dossier",
                ref="dossiers/a3f2.tar",
                sha="a" * 64,
                verified=True,
            )
        ],
        skipped=[SkippedNamespace(ref="deadbeef", reason="torn repo.json")],
        cache="miss",
        render="evidence · tags: edge-x, rv-data\n",
    )
    assert EvidenceBriefResult.model_validate(result.model_dump(mode="json")) == result


def test_period_result_round_trips_with_unconcluded() -> None:
    result = EvidencePeriodResult(
        computed_at="2026-07-08T06:12:00Z",
        conclusions=[],
        activity=[],
        envelopes=[],
        unconcluded=[
            UnconcludedItem(
                scope_kind="campaign",
                scope_id="widget-sweep-2025q4",
                completed_at="2025-12-01T00:00:00Z",
            )
        ],
        citations_status=[],
        skipped=[],
        cache="disabled",
        render="evidence period · since 2025-01-01\n",
    )
    round_tripped = EvidencePeriodResult.model_validate(result.model_dump(mode="json"))
    assert round_tripped == result
    assert round_tripped.unconcluded[0].scope_id == "widget-sweep-2025q4"


def test_brief_result_has_no_unconcluded_field() -> None:
    """``unconcluded`` is the period result's differentiator — absent on the brief."""
    assert "unconcluded" not in EvidenceBriefResult.model_fields
    assert "unconcluded" in EvidencePeriodResult.model_fields


# --- (b) the at-least-one-key refusal on the point spec ---------------------


def test_brief_spec_refuses_unkeyed_query() -> None:
    with pytest.raises(ValidationError, match="AT LEAST ONE"):
        EvidenceBriefSpec()


def test_brief_spec_refuses_empty_tags_and_null_lineage() -> None:
    with pytest.raises(ValidationError, match="AT LEAST ONE"):
        EvidenceBriefSpec(tags=[], lineage=None)


def test_brief_spec_accepts_tags_alone() -> None:
    spec = EvidenceBriefSpec(tags=["edge-x"])
    assert spec.lineage is None


def test_brief_spec_accepts_lineage_alone() -> None:
    spec = EvidenceBriefSpec(lineage="toy-run-abc123")
    assert spec.tags == []


def test_period_spec_needs_no_at_least_one_key() -> None:
    """A period is inherently time-keyed; empty tags is the whole-window view."""
    spec = EvidencePeriodSpec(since="2025-01-01T00:00:00Z")
    assert spec.tags == []


def test_period_spec_requires_since() -> None:
    with pytest.raises(ValidationError):
        EvidencePeriodSpec()  # type: ignore[call-arg]


# --- (c) unknown-field refusal (extra=forbid everywhere) --------------------


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (EvidenceBriefSpec, {"tags": ["x"], "since": "2025-01-01"}),
        (EvidencePeriodSpec, {"since": "2025-01-01", "lineage": "r"}),
        (
            EvidenceBriefResult,
            {"computed_at": "t", "cache": "miss", "render": "r", "verdict": "go"},
        ),
        (
            EvidencePeriodResult,
            {"computed_at": "t", "cache": "miss", "render": "r", "unconcluded": [], "extra": 1},
        ),
        (
            ConclusionLine,
            {
                "conclusion_id": "c",
                "ts": "t",
                "status": "current",
                "finding": "f",
                "metric": "sharpe",
            },
        ),
        (ActivityLine, {"tag": "t", "treatment": "arm"}),
        (EnvelopeLine, {"lineage": "l", "envelope": "e", "baseline": 0}),
        (
            CitationStatusLine,
            {
                "conclusion_id": "c",
                "kind": "run",
                "ref": "r",
                "sha": "s",
                "verified": True,
                "unit": 1,
            },
        ),
        (
            UnconcludedItem,
            {"scope_kind": "campaign", "scope_id": "s", "completed_at": "t", "holdout": True},
        ),
        (SkippedNamespace, {"ref": "r", "reason": "torn", "control": "x"}),
    ],
)
def test_models_forbid_unknown_fields(model: type[BaseModel], kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_citation_status_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        CitationStatusLine(conclusion_id="c", kind="backtest", ref="r", sha="s", verified=True)  # type: ignore[arg-type]


# --- (d) the forbidden-vocabulary walk over every model ---------------------


@pytest.mark.parametrize("model", _ALL_MODELS)
def test_wire_models_expose_no_domain_vocabulary(model: type[BaseModel]) -> None:
    names = _schema_property_names(model.model_json_schema())
    leaked = names & _FORBIDDEN_FIELD_NAMES
    assert not leaked, (
        f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
        "The evidence wire names MECHANISM only (counts, dates, shas, tags, envelope "
        "labels); a field named for a caller-owned role is the substrate-vs-semantics leak."
    )


# --- (e) opaque finding round-trip ------------------------------------------


@pytest.mark.parametrize(
    "finding",
    [
        "",
        "no alpha in 2025H1, envelope ±2%, n=4",
        'weird "quoted" prose\nwith newlines\tand tabs — and unicode ✓',
        "{'looks': 'like json'} but is opaque str",
    ],
)
def test_finding_is_opaque_and_round_trips(finding: str) -> None:
    line = ConclusionLine(
        conclusion_id="c",
        ts="2025-11-14T00:00:00Z",
        cited_shas=["a3f2c9d1"],
        status="current",
        finding=finding,
    )
    round_tripped = ConclusionLine.model_validate(line.model_dump(mode="json"))
    assert round_tripped.finding == finding


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
