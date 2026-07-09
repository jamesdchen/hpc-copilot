"""Wire-model tests for the ``challenge-status`` query (challenge-attestation T2).

Covers the contracts the T2 wire owes (the full contract-suite mirror is T9):
model-validation round-trips, the EXACTLY-ONE addressing enforcement (none,
multiple, incomplete subject pair), the forbidden-vocabulary walk over THESE
schemas (mirrored locally from ``tests/contracts/test_dossier_boundary.py``),
unknown-field refusal, and the status-literal equality pin (the SAME five T1
reduces to).
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from hpc_agent._wire.queries.challenge_status import (
    ChallengeEntry,
    ChallengeStatus,
    ChallengeStatusResult,
    ChallengeStatusSpec,
    ChallengeTarget,
    ChallengeVerdict,
    CitationStatusLine,
    ContestedCounts,
    SkippedNamespace,
)

# Domain-semantics vocabulary core must never name on the wire — the closed set
# mirrored from tests/contracts/test_dossier_boundary.py (kept inline so drift
# surfaces here; the full mirror lands in T9).
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


# --- (a) model validation round-trips ---------------------------------------


def test_spec_round_trips_by_challenge_id() -> None:
    spec = ChallengeStatusSpec(challenge_id="widget-conclusion-dissent-1")
    assert ChallengeStatusSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_spec_round_trips_by_content_sha() -> None:
    spec = ChallengeStatusSpec(content_sha="a" * 64)
    assert ChallengeStatusSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_spec_round_trips_by_subject_pair() -> None:
    spec = ChallengeStatusSpec(subject_kind="conclusion", subject_id="widget-jam-finding")
    assert ChallengeStatusSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_spec_fleet_flag_round_trips() -> None:
    spec = ChallengeStatusSpec(content_sha="b" * 64, fleet=True)
    assert spec.fleet is True
    assert ChallengeStatusSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_result_round_trips_full_shape() -> None:
    result = ChallengeStatusResult(
        computed_at="2026-07-08T00:00:00Z",
        challenges=[
            ChallengeEntry(
                challenge_id="widget-conclusion-dissent-1",
                status="open",
                filed_at="2026-07-07T12:00:00Z",
                target=ChallengeTarget(
                    kind="attestation",
                    subject_kind="conclusion",
                    subject_id="widget-jam-finding",
                    content_sha="c" * 8,
                ),
                resolution="found-current",
                grounds="the replication under seed 7 contradicts the jam-rate claim",
            ),
            ChallengeEntry(
                challenge_id="widget-batch-reg-dissent-2",
                status="dismissed",
                filed_at="2026-07-06T09:00:00Z",
                target=ChallengeTarget(
                    kind="dossier",
                    subject_kind="registration",
                    subject_id="widget-batch-42",
                    content_sha="d" * 8,
                ),
                resolution="found-superseded",
                grounds="the bundled sample count is below the declared floor",
                verdict=ChallengeVerdict(
                    verdict="dismissed",
                    reasoning="the floor was met once the partial samples are counted",
                    ts="2026-07-06T18:00:00Z",
                ),
            ),
        ],
        citations_status=[
            CitationStatusLine(
                challenge_id="widget-conclusion-dissent-1",
                kind="fingerprint",
                ref="7be4c0de",
                sha="e" * 64,
                verified=True,
            ),
        ],
        contested=ContestedCounts(
            open=1,
            dismissed=1,
            challenge_ids=["widget-conclusion-dissent-1", "widget-batch-reg-dissent-2"],
        ),
        skipped=[SkippedNamespace(ref="deadbeef", reason="torn")],
        render="# challenge-status\n\ncontested · widget-conclusion-dissent-1 · filed 2026-07-07\n",
        view_sha="f" * 8,
    )
    assert ChallengeStatusResult.model_validate(result.model_dump(mode="json")) == result


def test_result_empty_shape_defaults() -> None:
    """The no-challenge shape: empty lists + a zeroed contested block."""
    result = ChallengeStatusResult(
        computed_at="2026-07-08T00:00:00Z",
        render="# challenge-status\n\n(no standing challenges)\n",
        view_sha="a" * 8,
    )
    assert result.challenges == []
    assert result.citations_status == []
    assert result.skipped == []
    assert result.contested.open == 0
    assert result.contested.challenge_ids == []
    assert ChallengeStatusResult.model_validate(result.model_dump(mode="json")) == result


def test_entry_verdict_optional_when_open() -> None:
    entry = ChallengeEntry(
        challenge_id="c1",
        status="open",
        filed_at="2026-07-07T12:00:00Z",
        target=ChallengeTarget(
            kind="run", subject_kind="conclusion", subject_id="s", content_sha="a" * 8
        ),
        resolution="found-current",
        grounds="dissent prose",
    )
    assert entry.verdict is None
    assert ChallengeEntry.model_validate(entry.model_dump(mode="json")) == entry


# --- (b) EXACTLY-ONE addressing enforcement ---------------------------------


def test_spec_refuses_no_address() -> None:
    with pytest.raises(ValidationError, match="EXACTLY ONE"):
        ChallengeStatusSpec()


def test_spec_refuses_id_and_content_sha() -> None:
    with pytest.raises(ValidationError, match="EXACTLY ONE"):
        ChallengeStatusSpec(challenge_id="c1", content_sha="a" * 64)


def test_spec_refuses_id_and_subject_pair() -> None:
    with pytest.raises(ValidationError, match="EXACTLY ONE"):
        ChallengeStatusSpec(challenge_id="c1", subject_kind="conclusion", subject_id="s")


def test_spec_refuses_content_sha_and_subject_pair() -> None:
    with pytest.raises(ValidationError, match="EXACTLY ONE"):
        ChallengeStatusSpec(content_sha="a" * 64, subject_kind="conclusion", subject_id="s")


def test_spec_refuses_lone_subject_kind() -> None:
    """A subject half addresses nothing checkable — the R3 full-address rule."""
    with pytest.raises(ValidationError, match="ATOMIC pair"):
        ChallengeStatusSpec(subject_kind="conclusion")


def test_spec_refuses_lone_subject_id() -> None:
    with pytest.raises(ValidationError, match="ATOMIC pair"):
        ChallengeStatusSpec(subject_id="s")


# --- (c) forbidden-vocabulary walk over THESE schemas -----------------------


@pytest.mark.parametrize(
    "model",
    [
        ChallengeStatusSpec,
        ChallengeStatusResult,
        ChallengeEntry,
        ChallengeTarget,
        ChallengeVerdict,
        CitationStatusLine,
        ContestedCounts,
        SkippedNamespace,
    ],
)
def test_wire_models_expose_no_domain_vocabulary(model: type[BaseModel]) -> None:
    names = _schema_property_names(model.model_json_schema())
    leaked = names & _FORBIDDEN_FIELD_NAMES
    assert not leaked, (
        f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
        "The challenge wire names MECHANISM only (status, counts, shas, dates, opaque "
        "ids); a field named for a caller-owned role is the substrate-vs-semantics leak."
    )


# --- (d) unknown-field refusal (extra=forbid everywhere) --------------------


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (ChallengeStatusSpec, {"challenge_id": "c1", "role": "prod"}),
        (
            ChallengeStatusResult,
            {"computed_at": "t", "render": "r", "view_sha": "s", "verdict": "go"},
        ),
        (
            ChallengeTarget,
            {
                "kind": "attestation",
                "subject_kind": "conclusion",
                "subject_id": "s",
                "content_sha": "a" * 8,
                "metric": "sharpe",
            },
        ),
        (ContestedCounts, {"open": 1, "significance": 0.05}),
        (
            CitationStatusLine,
            {
                "challenge_id": "c1",
                "kind": "run",
                "ref": "r",
                "sha": "a",
                "verified": True,
                "unit": "x",
            },
        ),
    ],
)
def test_models_forbid_unknown_fields(model: type[BaseModel], kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_target_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ChallengeTarget(
            kind="backtest",  # type: ignore[arg-type]
            subject_kind="conclusion",
            subject_id="s",
            content_sha="a" * 8,
        )


# --- (e) status-literal equality pin ----------------------------------------


def test_challenge_status_literal_matches_the_five() -> None:
    """The reduced status vocabulary is EXACTLY T1's five (C-shape). The T9
    contract suite cross-pins this against ``state/challenges.py`` so the
    vocabulary lives in one place; here we pin the wire copy's five explicitly."""
    assert get_args(ChallengeStatus) == (
        "open",
        "upheld",
        "dismissed",
        "withdrawn",
        "superseded",
    )
