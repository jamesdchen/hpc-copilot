"""Wire-model tests for the ``verify-registration`` query (registration-kernel T2).

Covers the four contracts the T2 wire owes (the full contract-suite mirror is
T9): model validation round-trips, the either-or spec enforcement, the
forbidden-vocabulary walk over THESE schemas (mirrored locally from
``tests/contracts/test_dossier_boundary.py``), and unknown-field refusal.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from hpc_agent._wire.actions.verify_registration import (
    ChainEntry,
    DossierLeg,
    FieldsBlock,
    PrerequisiteLeg,
    PrerequisiteRequires,
    TemplateLeg,
    VerifyRegistrationResult,
    VerifyRegistrationSpec,
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


def test_spec_round_trips_by_registration_id() -> None:
    spec = VerifyRegistrationSpec(registration_id="widget-batch-42")
    assert VerifyRegistrationSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_spec_round_trips_by_run_id() -> None:
    spec = VerifyRegistrationSpec(run_id="toy-run-abc123")
    assert VerifyRegistrationSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_result_round_trips_full_shape() -> None:
    result = VerifyRegistrationResult(
        status="current",
        registration_id="widget-batch-42",
        registered_at="2026-07-08T00:00:00Z",
        dossier=DossierLeg(recorded_sha="a" * 8, recomputed_sha="a" * 8, drifted_stores=[]),
        template=TemplateLeg(status="current", recorded_sha="b" * 8, recomputed_sha="b" * 8),
        prerequisites=[
            PrerequisiteLeg(
                slot="repro-check",
                kind="reproduction",
                status="current",
                recorded_sha="c" * 8,
                recomputed_sha="c" * 8,
                evidence_note="block=reproduction attestor=human",
            )
        ],
        fields=FieldsBlock(
            declared=["widget-owner", "jam-threshold"],
            present=["widget-owner", "jam-threshold"],
            missing=[],
        ),
        brief="# registration current\n",
        view_sha="d" * 8,
    )
    assert VerifyRegistrationResult.model_validate(result.model_dump(mode="json")) == result


def test_result_absent_shape_has_null_legs() -> None:
    """An absent registration reports no legs (the degenerate shape T5 emits)."""
    result = VerifyRegistrationResult(status="absent")
    assert result.dossier is None
    assert result.template is None
    assert result.prerequisites == []
    assert result.fields.declared == []
    assert VerifyRegistrationResult.model_validate(result.model_dump(mode="json")) == result


def test_chain_entry_and_requires_round_trip() -> None:
    entry = ChainEntry(
        slot="repro-check",
        kind="reproduction",
        subject_id="toy-repro-run",
        content_sha="e" * 8,
        requires=PrerequisiteRequires(min_n=3, min_n_full=2, scales=["main"]).model_dump(
            mode="json", exclude_none=True
        ),
    )
    assert ChainEntry.model_validate(entry.model_dump(mode="json")) == entry
    assert entry.requires == {"min_n": 3, "min_n_full": 2, "scales": ["main"]}


# --- (b) either-or spec enforcement -----------------------------------------


def test_spec_refuses_both_addresses() -> None:
    with pytest.raises(ValidationError, match="EXACTLY ONE"):
        VerifyRegistrationSpec(registration_id="x", run_id="y")


def test_spec_refuses_neither_address() -> None:
    with pytest.raises(ValidationError, match="EXACTLY ONE"):
        VerifyRegistrationSpec()


# --- (c) forbidden-vocabulary walk over THESE schemas -----------------------


@pytest.mark.parametrize(
    "model",
    [
        VerifyRegistrationSpec,
        VerifyRegistrationResult,
        ChainEntry,
        PrerequisiteRequires,
        DossierLeg,
        TemplateLeg,
        PrerequisiteLeg,
        FieldsBlock,
    ],
)
def test_wire_models_expose_no_domain_vocabulary(model: type[BaseModel]) -> None:
    names = _schema_property_names(model.model_json_schema())
    leaked = names & _FORBIDDEN_FIELD_NAMES
    assert not leaked, (
        f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
        "The registration wire names MECHANISM only (status, legs, shas, counts); a "
        "field named for a caller-owned role is the substrate-vs-semantics leak."
    )


# --- (d) unknown-field refusal (extra=forbid everywhere) --------------------


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (VerifyRegistrationSpec, {"registration_id": "x", "role": "prod"}),
        (VerifyRegistrationResult, {"status": "absent", "verdict": "go"}),
        (
            ChainEntry,
            {
                "slot": "s",
                "kind": "attestation",
                "subject_id": "id",
                "content_sha": "a" * 8,
                "metric": "sharpe",
            },
        ),
        (PrerequisiteRequires, {"min_n": 1, "min_units": 2}),
        (DossierLeg, {"recorded_sha": "a", "recomputed_sha": "a", "treatment": "x"}),
    ],
)
def test_models_forbid_unknown_fields(model: type[BaseModel], kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_chain_entry_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ChainEntry(slot="s", kind="backtest", subject_id="id", content_sha="a" * 8)  # type: ignore[arg-type]
