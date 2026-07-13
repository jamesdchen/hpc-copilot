"""Wire-model tests for the two conformance verbs (live-conformance T2).

Covers the contracts the T2 wire owes: model round-trips, the window-selection
rule (mutual exclusion + at-least-one), the NO-SHA pin on the record spec, the
forbidden-vocabulary walk over THESE schemas, unknown-field refusal, and the
``tier_reason`` seven-member equality pin.

Vocabulary note (the deliberate divergence from the dossier walk). The dossier
boundary's ``_FORBIDDEN_FIELD_NAMES`` lists ``"baseline"`` because in the
EXPERIMENT-DESIGN domain a baseline is the control group — a semantics leak. In
live conformance ``baseline`` is the feature's own MECHANISM noun (the sealed,
point-in-time reference envelope — Shewhart's control-chart baseline; the design
doc names it ~40 times as core terminology). So the conformance denylist is
MARKET/trading vocabulary plus the experiment-design semantics MINUS ``baseline``:
a ``fill``/``order``/``position``/``pnl``-shaped name is the leak this pins, per
the live-conformance enforcement row ("No market vocabulary anywhere").
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from hpc_agent._wire.actions.conformance_record import (
    ConformanceRecordResult,
    ConformanceRecordSpec,
)
from hpc_agent._wire.queries.conformance_status import (
    ConformanceBaseline,
    ConformanceStatusResult,
    ConformanceStatusSpec,
    ConformanceTierReason,
    ConformanceWindow,
    KeyVerdictLine,
)

_ALL_MODELS: tuple[type[BaseModel], ...] = (
    ConformanceRecordSpec,
    ConformanceRecordResult,
    ConformanceStatusSpec,
    ConformanceStatusResult,
    KeyVerdictLine,
    ConformanceWindow,
    ConformanceBaseline,
)

# The closed seven-member tier_reason vocabulary (C-compare). Equality-pinned:
# adding or dropping a member must land here as a reviewed change.
_EXPECTED_TIER_REASONS = frozenset(
    {
        "within_envelope",
        "outside_envelope",
        "insufficient_window",
        "thin_baseline",
        "key_novelty",
        "label_novelty",
        "incomparable",
    }
)

# Market/trading vocabulary + experiment-design semantics MINUS "baseline" (a
# permitted SPC mechanism noun here — see module docstring). Any of these as a
# field NAME is the substrate-vs-semantics leak.
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        # experiment-design semantics (dossier set, MINUS "baseline")
        "control",
        "controls",
        "unit",
        "units",
        "metric",
        "metrics",
        "holdout",
        "treatment",
        "significance",
        "placebo",
        "anchor",
        # market / trading vocabulary — the live-conformance-specific leak
        "fill",
        "fills",
        "order",
        "orders",
        "position",
        "positions",
        "pnl",
        "trade",
        "trades",
        "yield",
        "venue",
        "ticker",
        "price",
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


# --- round-trips ------------------------------------------------------------


def test_record_spec_roundtrip_minimal_and_full() -> None:
    minimal = ConformanceRecordSpec(
        registration_id="sensor-7-cal",
        payload={"reading_c": 20.4, "cycles": 6, "ok": True, "tag": "run-a"},
        observed_at="2026-05-01T00:00:00Z",
    )
    assert minimal.labels == {}
    assert minimal.emitter is None
    full = ConformanceRecordSpec.model_validate(
        {
            "registration_id": "sensor-7-cal",
            "payload": {"reading_c": 20.4},
            "observed_at": "2026-05-01T00:00:00Z",
            "labels": {"bench": "north"},
            "emitter": "qc-daemon",
        }
    )
    assert ConformanceRecordSpec.model_validate(full.model_dump()) == full


def test_record_result_roundtrip() -> None:
    res = ConformanceRecordResult(
        registration_id="sensor-7-cal",
        content_sha="a" * 64,
        status_at_record="stale",
        observed_at="2026-05-01T00:00:00Z",
    )
    assert res.ledger_path is None
    assert ConformanceRecordResult.model_validate(res.model_dump()) == res


def test_status_spec_roundtrip_each_selection_mode() -> None:
    by_count = ConformanceStatusSpec(registration_id="sensor-7-cal", last_n=40)
    by_since = ConformanceStatusSpec(registration_id="sensor-7-cal", since="2026-05-01T00:00:00Z")
    by_span = ConformanceStatusSpec(
        registration_id="sensor-7-cal",
        since="2026-05-01T00:00:00Z",
        until="2026-06-01T00:00:00Z",
    )
    for spec in (by_count, by_since, by_span):
        assert ConformanceStatusSpec.model_validate(spec.model_dump()) == spec


def test_status_result_roundtrip() -> None:
    res = ConformanceStatusResult(
        registration_id="sensor-7-cal",
        overall="needs_verdict",
        keys=[
            KeyVerdictLine(
                key="reading_c",
                tier_reason="within_envelope",
                window_lo=20.1,
                window_hi=20.6,
                baseline_lo=20.0,
                baseline_hi=21.0,
                window_n=40,
                baseline_n=126,
            ),
            KeyVerdictLine(
                key="drift_ppm",
                tier_reason="key_novelty",
                window_n=40,
                baseline_n=0,
            ),
        ],
        window=ConformanceWindow(
            n=40, since="2026-05-01T00:00:00Z", until="2026-06-01T00:00:00Z", labels=["north"]
        ),
        baseline=ConformanceBaseline(n=126, sealed_at="2026-03-02T00:00:00Z"),
        declaration_echo={"keys": ["reading_c"], "min_window_n": 20, "review_horizon": None},
        render="reading_c: window [20.1, 20.6] vs registered [20.0, 21.0] ...",
    )
    assert ConformanceStatusResult.model_validate(res.model_dump()) == res


# --- the window-selection rule ---------------------------------------------


def test_last_n_and_timestamp_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="EITHER last_n"):
        ConformanceStatusSpec(registration_id="r", last_n=10, since="2026-05-01T00:00:00Z")
    with pytest.raises(ValidationError, match="EITHER last_n"):
        ConformanceStatusSpec(registration_id="r", last_n=10, until="2026-06-01T00:00:00Z")


def test_at_least_one_selection_required() -> None:
    # nothing supplied -> refused
    with pytest.raises(ValidationError, match="requires a window selection"):
        ConformanceStatusSpec(registration_id="r")
    # until alone is NOT a selection (no anchor) -> refused
    with pytest.raises(ValidationError, match="requires a window selection"):
        ConformanceStatusSpec(registration_id="r", until="2026-06-01T00:00:00Z")


def test_last_n_ge_1() -> None:
    with pytest.raises(ValidationError):
        ConformanceStatusSpec(registration_id="r", last_n=0)


# --- the no-sha pin ---------------------------------------------------------


def test_record_spec_carries_no_sha_field() -> None:
    """The record spec accepts NO caller sha — it is server-recomputed (C1)."""
    field_names = set(ConformanceRecordSpec.model_fields)
    assert not any("sha" in name for name in field_names), (
        f"ConformanceRecordSpec exposes a sha-shaped field {sorted(field_names)}; the "
        "content_sha is recomputed SERVER-SIDE and bound at append — a caller sha is a "
        "claim core ignores and must not be accepted on the wire (C1 recompute lock)."
    )
    # A caller trying to smuggle one in is refused by extra="forbid".
    with pytest.raises(ValidationError):
        ConformanceRecordSpec.model_validate(
            {
                "registration_id": "r",
                "payload": {"k": 1.0},
                "observed_at": "2026-05-01T00:00:00Z",
                "content_sha": "deadbeef",
            }
        )


# --- unknown-field refusal (extra="forbid") --------------------------------


@pytest.mark.parametrize("model", _ALL_MODELS, ids=[m.__name__ for m in _ALL_MODELS])
def test_models_forbid_unknown_fields(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"


# --- forbidden-vocabulary walk ---------------------------------------------


@pytest.mark.parametrize("model", _ALL_MODELS, ids=[m.__name__ for m in _ALL_MODELS])
def test_no_market_vocabulary_in_field_names(model: type[BaseModel]) -> None:
    names = _schema_property_names(model.model_json_schema())
    leaked = names & _FORBIDDEN_FIELD_NAMES
    assert not leaked, (
        f"{model.__name__} exposes a market/semantics field name {sorted(leaked)}. The "
        "conformance wire names mechanism only (window/baseline/envelope, opaque keys "
        "and labels); a fill/order/position/pnl-shaped name is the leak."
    )


# --- tier_reason equality pin ----------------------------------------------


def test_tier_reason_is_the_closed_seven_member_vocabulary() -> None:
    members = frozenset(get_args(ConformanceTierReason))
    assert members == _EXPECTED_TIER_REASONS, (
        f"tier_reason vocabulary drifted: {sorted(members)} vs "
        f"{sorted(_EXPECTED_TIER_REASONS)}. The seven-member fold (C-compare) is a "
        "closed set; a change lands here as a reviewed vocabulary edit."
    )
    # The wire field uses exactly this Literal (no widening).
    assert (
        frozenset(get_args(KeyVerdictLine.model_fields["tier_reason"].annotation))
        == _EXPECTED_TIER_REASONS
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
