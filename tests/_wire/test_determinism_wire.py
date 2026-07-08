"""Focused wire tests for the determinism-fingerprint shapes (T2).

Covers the four contracts T5 consumes: envelope/evidence/sample round-trips,
the ``tier_reason`` literal set pinned by equality, demand refusals (unknown
key, zero/negative n), the v1-receipt-parses-under-v2-models tolerance, and a
vocabulary walk (no domain-semantics field name leaks onto the wire).

Mirrors ``tests/contracts/test_dossier_boundary.py``'s forbidden-vocabulary
posture and ``tests/_wire/`` round-trip conventions.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from hpc_agent._wire.queries.determinism import (
    DeterminismSampleRecord,
    EnvelopeApplied,
    EnvelopeEvidence,
    EvidenceDemandSpec,
    SampleIdentity,
    SampleKeyDiff,
    TierReason,
)
from hpc_agent._wire.queries.verify_reproduction import (
    ReproductionReceipt,
    ReproKeyVerdict,
    VerifyReproductionResult,
)

# The domain-semantics vocabulary core must never name on the wire (field NAMES
# only). Kept inline so drift surfaces here, per the dossier-boundary house style.
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
    """Every ``properties`` key anywhere in a JSON schema, recursively (names only)."""
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


# ── literal-set pins ─────────────────────────────────────────────────────────


def test_tier_reason_literal_set_is_pinned() -> None:
    """The tier_reason vocabulary is EXACTLY the D-verdict-wire set — equality,
    so adding or dropping a member lands here as a reviewed change."""
    assert set(get_args(TierReason)) == {
        "exact",
        "within_evidenced_envelope",
        "within_thin_envelope",
        "outside_thin_envelope",
        "outside_evidenced_envelope",
        "caller_override",
    }


def test_result_stage_reached_gains_the_tiered_verdicts() -> None:
    """v2 widens the overall verdict with auto_cleared + needs_verdict, keeping v1."""
    field = VerifyReproductionResult.model_fields["stage_reached"]
    assert set(get_args(field.annotation)) == {
        "match",
        "mismatch",
        "incomparable",
        "auto_cleared",
        "needs_verdict",
    }


# ── round-trips ──────────────────────────────────────────────────────────────


def _evidence(**over: Any) -> EnvelopeEvidence:
    base: dict[str, Any] = dict(
        n=3,
        n_full=2,
        n_partial=1,
        scales=["main"],
        clusters=["hoffman2"],
        same_submission_only=False,
    )
    base.update(over)
    return EnvelopeEvidence(**base)


def _envelope(**over: Any) -> EnvelopeApplied:
    base: dict[str, Any] = dict(
        lo=1.0, hi=1.1, rel_spread=0.1, evidence=_evidence().model_dump(mode="json")
    )
    base["class"] = over.pop("class_", "stochastic")
    base.update(over)
    return EnvelopeApplied.model_validate(base)


def test_envelope_evidence_roundtrips_with_excluded_unadmitted_default() -> None:
    ev = _evidence()
    dumped = ev.model_dump(mode="json")
    assert dumped["excluded_unadmitted"] == 0  # no-silent-caps disclosure default
    assert EnvelopeEvidence(**dumped) == ev


def test_envelope_applied_emits_class_alias_on_dump() -> None:
    """The wire key is ``class`` (the alias), not ``envelope_class`` — dump WITHOUT
    by_alias must still emit ``class`` (serialize_by_alias=True)."""
    env = _envelope(class_="stochastic")
    dumped = env.model_dump(mode="json")
    assert "class" in dumped and "envelope_class" not in dumped
    assert dumped["class"] == "stochastic"
    # Round-trips back through the alias.
    assert EnvelopeApplied.model_validate(dumped) == env
    # ``class`` is the schema property name too.
    assert "class" in _schema_property_names(EnvelopeApplied.model_json_schema())


def test_sample_record_roundtrips_verbatim() -> None:
    rec = DeterminismSampleRecord(
        ts="2026-07-08T00:00:00Z",
        subject_id="a" * 64,
        content_sha="b" * 64,
        identity=SampleIdentity(cmd_sha="a" * 64, tasks_py_sha="c" * 64, executor="train.py"),
        source="double-canary",
        run_ids=["run-a", "run-a-canary2"],
        cluster="hoffman2",
        scale="canary",
        verdict="auto_cleared",
        same_submission=True,
        per_key=[
            SampleKeyDiff(
                key="x.y", a=1.0, b=1.0002, abs_diff=0.0002, rel_diff=0.0002, static_class="float"
            )
        ],
    )
    dumped = rec.model_dump(mode="json")
    assert dumped["schema_version"] == 1
    assert dumped["subject_kind"] == "determinism-fingerprint"
    assert DeterminismSampleRecord(**dumped) == rec


# ── demand refusals ──────────────────────────────────────────────────────────


def test_demand_minimal_and_full() -> None:
    assert EvidenceDemandSpec(min_n=1).scales == []
    d = EvidenceDemandSpec(min_n=3, min_n_full=2, scales=["main"], clusters=["hoffman2"])
    assert d.min_n_full == 2


def test_demand_refuses_unknown_key() -> None:
    with pytest.raises(ValidationError):
        EvidenceDemandSpec(min_n=1, min_samples=5)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [0, -1])
def test_demand_refuses_zero_or_negative_n(bad: int) -> None:
    with pytest.raises(ValidationError):
        EvidenceDemandSpec(min_n=bad)


def test_demand_refuses_zero_min_n_full() -> None:
    with pytest.raises(ValidationError):
        EvidenceDemandSpec(min_n=1, min_n_full=0)


# ── v1-parses-under-v2 tolerance ─────────────────────────────────────────────


def test_v1_receipt_parses_under_v2_models() -> None:
    """A schema_version-1 receipt line — no envelope/tier on keys, no partiality
    fields, overall in the v1 set — must parse UNCHANGED under the v2 models.
    The ledger is append-only; old lines remain readable."""
    v1_line = {
        "ts": "2026-07-06T00:00:00Z",
        "schema_version": 1,
        "original": {"run_id": "orig", "cmd_sha": "a" * 64},
        "repro": {"run_id": "repro", "cmd_sha": "a" * 64},
        "tolerance_spec": None,
        "per_key": [
            {
                "key": "loss",
                "original": 1.0,
                "repro": 1.0,
                "abs_diff": 0.0,
                "rel_diff": 0.0,
                "verdict": "match",
                "tolerance_applied": None,
            }
        ],
        "overall": "match",
        "sources": {"original_artifact": "aggregate", "repro_artifact": "aggregate"},
    }
    receipt = ReproductionReceipt.model_validate(v1_line)
    assert receipt.schema_version == 1
    assert receipt.partial is False
    assert receipt.task_indices is None
    key = receipt.per_key[0]
    assert key.envelope_applied is None
    assert key.tier_reason is None


def test_v2_key_carries_envelope_and_tier() -> None:
    key = ReproKeyVerdict(
        key="loss",
        original=1.0,
        repro=1.004,
        abs_diff=0.004,
        rel_diff=0.004,
        verdict="mismatch",
        envelope_applied=_envelope(class_="stochastic", hi=1.003, rel_spread=0.003),
        tier_reason="outside_evidenced_envelope",
    )
    assert ReproKeyVerdict(**key.model_dump(mode="json")) == key


# ── vocabulary walk ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        EnvelopeEvidence,
        EnvelopeApplied,
        EvidenceDemandSpec,
        SampleIdentity,
        SampleKeyDiff,
        DeterminismSampleRecord,
        ReproKeyVerdict,
        ReproductionReceipt,
        VerifyReproductionResult,
    ],
)
def test_no_domain_vocabulary_leaks_onto_the_wire(model: Any) -> None:
    """No determinism-fingerprint model exposes a domain-semantics field NAME.

    Mechanism nouns only — key/lo/hi/rel_spread/n/scale/cluster — never a metric
    name or a caller role. Mirrors the dossier boundary's forbidden set.
    """
    names = _schema_property_names(model.model_json_schema())
    leaked = names & _FORBIDDEN_FIELD_NAMES
    assert not leaked, f"{model.__name__} leaks domain vocabulary {sorted(leaked)}"
