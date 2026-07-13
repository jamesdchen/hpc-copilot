"""Tests for the ``_wire`` suffix / schema-reachability lint.

Pins these invariants (mirrors ``test_lint_telemetry_labels.py``):

1. The real tree passes — every public ``_wire`` model reaches an emitted schema
   or is a declared helper today. This is the coupling test: it fails if a new
   ``*Spec`` model stops emitting or a schema file goes missing.
2. The lint can actually FIRE:
   * a registered-suffix model (``*Spec``) that resolves to no schema (the
     "silently emits nothing" case a helper-exclusion or missing definition causes),
   * an I/O-shaped *third suffix* (``*Request``) that isn't a registered emitting
     suffix (the exact "third suffix emits nothing" trap the plan names), and
   * a resolved model whose emitted schema *file* is missing on disk (the
     ``schema_for() -> None`` degrade-on-rename case).
3. Non-tautological: a plain non-I/O helper suffix (``*Line``) does NOT fire, and
   an explicitly allowlisted helper name passes.
"""

from __future__ import annotations

import importlib.util
import sys

from pydantic import BaseModel

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_wire_suffix", REPO_ROOT / "scripts" / "lint_wire_suffix.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_wire_suffix"] = lint
_SPEC.loader.exec_module(lint)


_MOD = "hpc_agent._wire.actions.synthetic"


# --- 1. real tree is clean --------------------------------------------------


def test_real_tree_is_clean() -> None:
    """Every public wire model reaches an emitted schema or is a declared helper."""
    assert lint.main() == 0


# --- 2a. a registered-suffix model that emits nothing FIRES -----------------


def test_unemitted_spec_model_fires(capsys) -> None:
    """A ``*Spec`` model absent from the registry and not allowlisted is a loud
    violation — the ``schema silently emits nothing`` case for a registered suffix."""
    bs = lint._load_build_schemas()

    class GhostSpec(BaseModel):  # not in SCHEMA_REGISTRY, not in _HELPER_NAMES
        x: int

    violations = lint.find_violations([(GhostSpec, _MOD)], bs)
    assert any("GhostSpec" in v and "Spec" in v for v in violations), violations


# --- 2b. an I/O-shaped THIRD suffix FIRES (the plan's named trap) -----------


def test_third_suffix_model_fires_via_main(monkeypatch) -> None:
    """A model named ``*Request`` — an I/O-shaped suffix that is NOT a registered
    emitting suffix — makes ``main()`` exit 1. This is the "a third suffix emits
    nothing" failure the lint exists to convert into a loud one."""

    class GhostRequest(BaseModel):
        x: int

    real_iter = lint._iter_public_wire_models

    def _iter_with_ghost():
        yield from real_iter()
        yield GhostRequest, _MOD

    monkeypatch.setattr(lint, "_iter_public_wire_models", _iter_with_ghost)
    assert lint.main() == 1


# --- 2c. a resolved model whose schema file is missing FIRES ----------------


def test_missing_schema_file_fires(monkeypatch, tmp_path) -> None:
    """A model in the registry whose emitted schema file is absent on disk fires —
    the degrade-to-None-on-rename case ``schema_for()`` hides."""
    bs = lint._load_build_schemas()

    class RealResult(BaseModel):
        x: int

    # Registry entry pointing at a file that does not exist under tmp_path.
    fake_registry = [(RealResult, "real.output.json", tmp_path)]
    monkeypatch.setattr(bs, "SCHEMA_REGISTRY", fake_registry)

    violations = lint.find_violations([], bs)
    assert any("real.output.json" in v and "missing" in v for v in violations), violations


# --- 3. non-tautological: helpers and plain suffixes stay clean -------------


def test_plain_helper_suffix_passes() -> None:
    """A plain non-I/O helper suffix (``*Line``) is out of scope and does NOT fire —
    proving the fires above are real, not "everything unresolved fails"."""
    bs = lint._load_build_schemas()

    class ActivityLine(BaseModel):  # embedded sub-model, no schema by design
        text: str

    assert lint.find_violations([(ActivityLine, _MOD)], bs) == []


def test_allowlisted_helper_passes() -> None:
    """A registered-suffix name in ``_HELPER_NAMES`` (e.g. ``MpiSpec``) passes even
    though it emits no standalone schema."""
    bs = lint._load_build_schemas()

    # Reuse the real allowlisted name so the test tracks the actual SoT.
    helper_name = next(iter(bs._HELPER_NAMES))
    ghost = type(helper_name, (BaseModel,), {"__annotations__": {"x": int}})

    assert lint.find_violations([(ghost, _MOD)], bs) == []


def test_reserved_suffix_helper_passes() -> None:
    """A name in ``_RESERVED_SUFFIX_HELPERS`` (e.g. ``SpawnRequest``) passes despite
    its I/O-shaped suffix — the deliberate declared-helper escape hatch."""
    bs = lint._load_build_schemas()
    ghost = type("SpawnRequest", (BaseModel,), {"__annotations__": {"x": int}})
    assert lint.find_violations([(ghost, _MOD)], bs) == []
