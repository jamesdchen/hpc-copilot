"""Wire-model contract tests for the domain-pack verbs (domain-packs T3).

Pins the three properties the plan names for the pack wire surface:

1. **round-trips + strictness** — every model dumps and re-validates equal, and
   ``extra="forbid"`` refuses an unknown field.
2. **no domain vocabulary on the wire** — the ``_schema_property_names``
   recursive walk (mirrored from ``tests/contracts/test_dossier_boundary.py``;
   the authoritative contract-suite mirror is T11) finds no field NAME from the
   forbidden domain-semantics set on any pack model.
3. **NO caller-suppliable sha** — ``PackRecordReceiptSpec``'s field-name set is
   EXACTLY ``{pack, slot, checked, passed, evidence}``: no ``content_sha`` /
   ``manifest_sha`` / per-file sha a caller could assert (the enforcement map's
   "receipt shas are server-computed" row, one layer up from the v1 laundering
   hole).
4. **evidence opacity** — an arbitrary nested dict survives the receipt-spec
   round-trip untouched.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from hpc_agent._wire.actions.pack_bind import (
    PackBindResult,
    PackBindSpec,
    PackFileEntry,
)
from hpc_agent._wire.actions.pack_record_receipt import (
    PackRecordReceiptResult,
    PackRecordReceiptSpec,
)
from hpc_agent._wire.actions.pack_status import (
    PackBind,
    PackDanglingReference,
    PackSlotStatus,
    PackStatusEntry,
    PackStatusResult,
    PackStatusSpec,
    PackUnfillableRequirement,
)

# Mirrors tests/contracts/test_dossier_boundary.py::_FORBIDDEN_FIELD_NAMES — the
# domain-semantics vocabulary core must never name on the wire (T11 hosts the
# authoritative contract-suite copy; this local mirror keeps T3 self-checking).
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

_ALL_MODELS: tuple[type[BaseModel], ...] = (
    PackBindSpec,
    PackFileEntry,
    PackBindResult,
    PackRecordReceiptSpec,
    PackRecordReceiptResult,
    PackStatusSpec,
    PackBind,
    PackSlotStatus,
    PackUnfillableRequirement,
    PackDanglingReference,
    PackStatusEntry,
    PackStatusResult,
)


def _schema_property_names(schema: dict[str, Any]) -> set[str]:
    """Every property NAME anywhere in a JSON schema, recursively.

    Copied from the dossier boundary suite: walks the whole schema object
    (top-level ``properties`` plus every nested model under ``$defs`` / ``items``)
    collecting property keys. Names only — descriptions/titles are not walked, so
    domain words in prose never trip the forbidden-vocabulary test.
    """
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


# --- round-trips + strictness ----------------------------------------------


def test_pack_bind_round_trips() -> None:
    spec = PackBindSpec(manifest="packs/toy-widgets/manifest.json", pack="toy-widgets")
    assert PackBindSpec.model_validate(spec.model_dump()) == spec

    result = PackBindResult(
        pack="toy-widgets",
        version="1.2.0",
        manifest_sha="a" * 64,
        files=[PackFileEntry(path="vocab/readers.json", sha256="b" * 64)],
        seams=["reader_calls"],
    )
    assert PackBindResult.model_validate(result.model_dump()) == result


def test_pack_bind_spec_manifest_only_minimal() -> None:
    """The spec is minimal: manifest alone is a valid bind (pack is optional)."""
    spec = PackBindSpec(manifest="m.json")
    assert spec.pack is None


def test_pack_record_receipt_round_trips() -> None:
    spec = PackRecordReceiptSpec(
        pack="toy-widgets",
        slot="widget-audit",
        checked=["data/widgets.csv"],
        passed=True,
        evidence={"rows": 30},
    )
    assert PackRecordReceiptSpec.model_validate(spec.model_dump()) == spec

    result = PackRecordReceiptResult(
        pack="toy-widgets",
        version="1.2.0",
        manifest_sha="a" * 64,
        slot="widget-audit",
        content_sha="c" * 64,
        passed=True,
    )
    assert PackRecordReceiptResult.model_validate(result.model_dump()) == result


def test_pack_status_round_trips() -> None:
    spec = PackStatusSpec(pack="toy-widgets")
    assert PackStatusSpec.model_validate(spec.model_dump()) == spec
    assert PackStatusSpec().pack is None

    result = PackStatusResult(
        packs={
            "toy-widgets": PackStatusEntry(
                bind=PackBind(
                    pack="toy-widgets",
                    version="1.2.0",
                    manifest_sha="a" * 64,
                    bound_at="2026-07-08T00:00:00Z",
                ),
                slots=[
                    PackSlotStatus(slot="widget-audit", status="current", passed=True),
                    PackSlotStatus(slot="stats-check", status="missing"),
                ],
                unfillable=[
                    PackUnfillableRequirement(
                        slot="stats-check", pack="toy-widgets", reason="not in fills_slots"
                    )
                ],
                dangling=[PackDanglingReference(reason="manifest missing", path="m.json")],
            )
        }
    )
    assert PackStatusResult.model_validate(result.model_dump()) == result


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (PackBindSpec, {"manifest": "m.json"}),
        (PackRecordReceiptSpec, {"pack": "p", "slot": "s", "passed": True}),
        (PackStatusSpec, {}),
    ],
)
def test_specs_forbid_unknown_fields(model: type[BaseModel], kwargs: dict[str, Any]) -> None:
    model(**kwargs)  # baseline: valid
    with pytest.raises(ValidationError):
        model(**{**kwargs, "bogus": "x"})


# --- no domain vocabulary on the wire --------------------------------------


@pytest.mark.parametrize("model", _ALL_MODELS, ids=[m.__name__ for m in _ALL_MODELS])
def test_pack_models_expose_no_domain_vocabulary(model: type[BaseModel]) -> None:
    names = _schema_property_names(model.model_json_schema())
    leaked = names & _FORBIDDEN_FIELD_NAMES
    assert not leaked, (
        f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
        "Pack wire models carry mechanism nouns only (pack, slot, manifest, sha, "
        "receipt, ...); a meaning-bearing field is the substrate-vs-semantics leak."
    )


# --- NO caller-suppliable sha (the enforcement row) ------------------------


def test_record_receipt_spec_has_no_caller_sha() -> None:
    """``PackRecordReceiptSpec`` field-name set is EXACTLY the sha-free set.

    Every sha (``content_sha``, ``manifest_sha``, a per-file sha) is server-
    computed; none may be caller-suppliable. Asserting the exact field set (not
    just "no sha substring") is the strongest form: a NEW field of any name
    lands here as a deliberate review, and any ``*sha*`` field fails outright.
    """
    fields = set(PackRecordReceiptSpec.model_fields)
    assert fields == {"pack", "slot", "checked", "passed", "evidence"}, (
        f"PackRecordReceiptSpec field set drifted to {sorted(fields)}. Receipt shas "
        "are server-computed (the enforcement row); the spec carries no sha field."
    )
    assert not any("sha" in name.lower() for name in fields), (
        "a caller-suppliable sha field appeared on PackRecordReceiptSpec — the v1 "
        "receipt-laundering hole, re-opened one layer up."
    )


# --- evidence opacity -------------------------------------------------------


def test_receipt_evidence_is_opaque_and_survives_round_trip() -> None:
    nested: dict[str, Any] = {
        "checks": [{"name": "rowcount", "ok": True, "detail": {"n": 30, "cols": ["a", "b"]}}],
        "note": "arbitrary caller payload",
    }
    spec = PackRecordReceiptSpec(pack="p", slot="s", passed=True, evidence=nested)
    assert PackRecordReceiptSpec.model_validate(spec.model_dump()).evidence == nested

    # A bare string is equally valid opaque evidence.
    spec_str = PackRecordReceiptSpec(pack="p", slot="s", passed=False, evidence="free text")
    assert spec_str.evidence == "free text"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
