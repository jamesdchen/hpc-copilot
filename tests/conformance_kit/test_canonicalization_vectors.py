"""Unit tests for the K2 canonicalization VECTORS themselves.

The conformance module (``hpc_agent.conformance.test_canonicalization``) asserts
a candidate reproduces the pinned digests. This file is the vectors' own test:
it proves (a) hpc-agent's reference implementation passes 100% of them, (b) each
pinned field is internally self-consistent, and (c) — the guard-can-fire check —
the cases tagged ``divergence_from`` REALLY diverge from that named reference
(JCS / default-json / strict-json), so a silently-swapped implementation would
be caught loudly.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from hpc_agent.state.audit_source import normalize_source, sha256_normalized
from hpc_agent.state.determinism import canonical_sha

_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hpc_agent"
    / "conformance"
    / "fixtures"
    / "canonicalization"
)

_SPECIAL_FLOATS = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}


def _decode_specials(node: object) -> object:
    if isinstance(node, dict):
        if set(node) == {"__float__"}:
            return _SPECIAL_FLOATS[node["__float__"]]
        return {k: _decode_specials(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_decode_specials(v) for v in node]
    return node


def _load(name: str) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        json.loads((_FIXTURES / name).read_text(encoding="utf-8")),
    )


_CANON = _load("canon_vectors.json")["cases"]
_NORM = _load("normalize_vectors.json")["cases"]
_CANON_IDS = [c["name"] for c in _CANON]
_NORM_IDS = [c["name"] for c in _NORM]


def test_fixtures_present_and_nonempty() -> None:
    assert (_FIXTURES / "canon_vectors.json").is_file()
    assert (_FIXTURES / "normalize_vectors.json").is_file()
    assert len(_CANON) >= 10
    assert len(_NORM) >= 8


# --- (a) reference implementation passes 100% --------------------------------


@pytest.mark.parametrize("case", _CANON, ids=_CANON_IDS)
def test_reference_canonical_sha_reproduces_pin(case: dict) -> None:
    value = _decode_specials(case["value"])
    assert canonical_sha(value) == case["expected_sha256"], case["name"]


@pytest.mark.parametrize("case", _NORM, ids=_NORM_IDS)
def test_reference_normalized_sha_reproduces_pin(case: dict) -> None:
    raw = base64.b64decode(case["input_b64"]).decode("utf-8")
    assert sha256_normalized(raw) == case["expected_sha256"], case["name"]


# --- (b) each pin is internally self-consistent ------------------------------


@pytest.mark.parametrize("case", _CANON, ids=_CANON_IDS)
def test_canon_pin_self_consistent(case: dict) -> None:
    value = _decode_specials(case["value"])
    # the recorded canonical string is what json.dumps actually emits
    produced = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert produced == case["expected_canonical"], case["name"]
    # and it hashes to the recorded digest
    digest = hashlib.sha256(case["expected_canonical"].encode("utf-8")).hexdigest()
    assert digest == case["expected_sha256"], case["name"]


@pytest.mark.parametrize("case", _NORM, ids=_NORM_IDS)
def test_normalize_pin_self_consistent(case: dict) -> None:
    raw = base64.b64decode(case["input_b64"]).decode("utf-8")
    normed = normalize_source(raw)
    assert normed.encode("utf-8") == base64.b64decode(case["expected_normalized_b64"]), case["name"]
    digest = hashlib.sha256(normed.encode("utf-8")).hexdigest()
    assert digest == case["expected_sha256"], case["name"]


# --- (c) the guard can fire: divergences REALLY diverge from their reference --


def _jcs_like(obj: object) -> str:
    """A minimal RFC-8785/JCS-shaped reference used ONLY to prove divergence
    for the ``jcs`` cases (astral key order, integral-float number canon).

    Deliberate JCS behaviours that differ from the harness form:
    * keys sorted by UTF-16 code UNIT (not Unicode code point);
    * ECMAScript number canon for integral floats (1.0 -> "1", -0.0 -> "0").
    Only exercises the value shapes the ``jcs`` vectors use (int/float/str keyed
    objects) -- it is NOT a general JCS implementation and does not attempt
    ECMAScript exponent formatting.
    """

    def enc(node: object) -> str:
        if isinstance(node, bool):
            return "true" if node else "false"
        if isinstance(node, float):
            if node == int(node):
                return str(int(node))  # 1.0 -> "1", -0.0 -> "0"
            return repr(node)
        if isinstance(node, int):
            return str(node)
        if isinstance(node, str):
            return json.dumps(node, ensure_ascii=False)
        if isinstance(node, list):
            return "[" + ",".join(enc(v) for v in node) + "]"
        if isinstance(node, dict):
            items = sorted(node.items(), key=lambda kv: kv[0].encode("utf-16-be"))
            return "{" + ",".join(f"{enc(k)}:{enc(v)}" for k, v in items) + "}"
        raise TypeError(type(node))

    return enc(obj)


def test_at_least_one_astral_key_divergence_present() -> None:
    """The headline divergence — astral vs BMP key order — must be covered."""
    jcs_names = {c["name"] for c in _CANON if c["divergence_from"] == "jcs"}
    assert "astral_vs_bmp_key_order" in jcs_names


def test_every_divergence_reference_is_covered() -> None:
    """All three divergence references appear — the vector set exercises each
    kind of implementation the harness form deliberately parts ways with."""
    refs = {c["divergence_from"] for c in _CANON if c["divergence_from"]}
    assert refs == {"jcs", "default-json", "strict-json"}


_JCS = [c for c in _CANON if c["divergence_from"] == "jcs"]


@pytest.mark.parametrize("case", _JCS, ids=[c["name"] for c in _JCS])
def test_jcs_cases_actually_diverge_from_jcs(case: dict) -> None:
    """Each ``jcs`` case produces DIFFERENT bytes under the JCS-shaped
    reference — proving the pin guards a real contract divergence (the guard
    can fire), not a coincidental agreement."""
    value = _decode_specials(case["value"])
    assert _jcs_like(value) != case["expected_canonical"], (
        f"{case['name']} is flagged divergence_from=jcs but the JCS-shaped "
        f"reference produced identical bytes: {case['expected_canonical']!r}"
    )


_DEFAULT = [c for c in _CANON if c["divergence_from"] == "default-json"]


@pytest.mark.parametrize("case", _DEFAULT, ids=[c["name"] for c in _DEFAULT])
def test_default_json_cases_diverge_from_ensure_ascii(case: dict) -> None:
    """Each ``default-json`` case differs from Python's DEFAULT json.dumps
    (ensure_ascii=True) — the escaping the harness form suppresses."""
    value = _decode_specials(case["value"])
    default_form = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert default_form != case["expected_canonical"], case["name"]


_STRICT = [c for c in _CANON if c["divergence_from"] == "strict-json"]


@pytest.mark.parametrize("case", _STRICT, ids=[c["name"] for c in _STRICT])
def test_strict_json_cases_are_rejected_by_strict_parsers(case: dict) -> None:
    """Each ``strict-json`` case's canonical form is NOT valid strict JSON
    (NaN/Infinity) — a strict parser rejects what the harness form admits."""

    def _reject(token: str) -> float:
        raise ValueError(f"strict JSON forbids {token}")

    with pytest.raises(ValueError):
        json.loads(case["expected_canonical"], parse_constant=_reject)
