"""Conformance kit K2 — the sha-canonicalization module.

Asserts a CANDIDATE canonicalization implementation reproduces, byte-for-byte,
the harness-contract sha form pinned in the committed vectors under
``fixtures/canonicalization/``:

* the JSON canonical form —
  ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` ->
  UTF-8 -> SHA-256 lowercase hex (``state/determinism.py::canonical_sha``),
  including the vectors where this DELIBERATELY diverges from RFC 8785 / JCS
  (astral-vs-BMP key order, integral-float ``1.0`` retention, ``NaN``/
  ``Infinity`` admission, ``ensure_ascii=False`` raw UTF-8);
* the source-text normalization —
  ``state/audit_source.py::normalize_source`` / ``::sha256_normalized`` (CRLF /
  lone-CR unification, per-line trailing-whitespace strip).

The default candidate is hpc-agent's own helpers. The module is PARAMETERIZED
over candidates so a second harness's implementation can be swapped in.

K1 seam: this module imports nothing from ``conftest.py`` / ``adapter.py`` at
collection time. When the K1 wave lands, its ``conftest.py`` may inject a second
``CanonCandidate`` (built from ``--harness-adapter``) by overriding the
module-level ``canon_candidates`` fixture; the standalone fallback below yields
only the built-in candidate so this file is runnable on its own via
``pytest src/hpc_agent/conformance/test_canonicalization.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

_FIXTURES = Path(__file__).parent / "fixtures" / "canonicalization"


@dataclass(frozen=True)
class CanonCandidate:
    """A canonicalization implementation under test.

    ``name`` — the implementation's report identity.
    ``canonical_sha`` — obj -> SHA-256 hex over the contract JSON form.
    ``sha256_normalized`` — text -> SHA-256 hex over the normalized source.
    """

    name: str
    canonical_sha: Callable[[object], str]
    sha256_normalized: Callable[[str], str]


def _builtin_candidate() -> CanonCandidate:
    # Imported lazily so a foreign-harness run that has replaced these need not
    # keep them importable (and so collection never depends on state internals
    # beyond the reference).
    from hpc_agent.state.audit_source import sha256_normalized
    from hpc_agent.state.determinism import canonical_sha

    return CanonCandidate(
        name="hpc-agent",
        canonical_sha=canonical_sha,
        sha256_normalized=sha256_normalized,
    )


@pytest.fixture
def canon_candidates() -> list[CanonCandidate]:
    """The candidate implementations to certify.

    Standalone fallback: the built-in hpc-agent helpers. K1's ``conftest.py``
    may override this fixture to also yield a second harness's implementation
    resolved from ``--harness-adapter`` (the parametrization below fans out over
    whatever this returns).
    """
    return [_builtin_candidate()]


# --- vector loading ----------------------------------------------------------

# Sentinel-decoded floats strict JSON cannot carry (kept portable in the
# fixture as {"__float__": "nan"|"inf"|"-inf"}).
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


def _canon_cases() -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", _load("canon_vectors.json")["cases"])


def _normalize_cases() -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", _load("normalize_vectors.json")["cases"])


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [c["name"] for c in cases]


# --- assertions --------------------------------------------------------------


@pytest.mark.parametrize("case", _canon_cases(), ids=_ids(_canon_cases()))
def test_candidate_canonical_sha_matches_vector(
    case: dict, canon_candidates: list[CanonCandidate]
) -> None:
    """Every candidate reproduces the pinned canonical-sha for each vector,
    including the deliberate JCS divergences."""
    value = _decode_specials(case["value"])
    expected = case["expected_sha256"]
    for cand in canon_candidates:
        got = cand.canonical_sha(value)
        assert got == expected, (
            f"[{cand.name}] canonical_sha diverged on {case['name']!r}: "
            f"got {got}, expected {expected} "
            f"(harness form is {case['expected_canonical']!r}); {case['note']}"
        )


@pytest.mark.parametrize("case", _canon_cases(), ids=_ids(_canon_cases()))
def test_pinned_canonical_form_hashes_to_pinned_digest(case: dict) -> None:
    """The recorded canonical STRING hashes to the recorded digest — the
    byte-exact pin a foreign implementation reproduces (independent of any
    candidate, so a wrong candidate is diagnosed against the true bytes)."""
    digest = hashlib.sha256(case["expected_canonical"].encode("utf-8")).hexdigest()
    assert digest == case["expected_sha256"], case["name"]


@pytest.mark.parametrize("case", _normalize_cases(), ids=_ids(_normalize_cases()))
def test_candidate_normalized_sha_matches_vector(
    case: dict, canon_candidates: list[CanonCandidate]
) -> None:
    """Every candidate reproduces the pinned normalized-source sha (CRLF /
    lone-CR unification + per-line trailing-whitespace strip)."""
    raw = base64.b64decode(case["input_b64"]).decode("utf-8")
    expected = case["expected_sha256"]
    for cand in canon_candidates:
        got = cand.sha256_normalized(raw)
        assert got == expected, (
            f"[{cand.name}] sha256_normalized diverged on {case['name']!r}: "
            f"got {got}, expected {expected}; {case['note']}"
        )


@pytest.mark.parametrize("case", _normalize_cases(), ids=_ids(_normalize_cases()))
def test_pinned_normalized_bytes_hash_to_pinned_digest(case: dict) -> None:
    """The recorded normalized bytes hash to the recorded digest (the pin is
    self-consistent, independent of any candidate)."""
    normed = base64.b64decode(case["expected_normalized_b64"])
    digest = hashlib.sha256(normed).hexdigest()
    assert digest == case["expected_sha256"], case["name"]
