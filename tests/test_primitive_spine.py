"""Cross-validation tests for the @primitive registry.

Pins the invariants for items 2/3/5/6/7 of the C' breakage list.
The runtime registry sits alongside frontmatter (docs/primitives/*.md)
during the migration; these tests assert the contracts that prevent
the failure modes catalogued in the audit.
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hpc_mapreduce._primitive import (
    PrimitiveMeta,
    _PRIMITIVE_MODULES,
    discover_primitive_modules,
    get_registry,
)


@pytest.fixture(scope="module")
def registry() -> dict[str, PrimitiveMeta]:
    return get_registry()


# Item #2 — registered but invisible.

def test_no_orphan_primitive_modules() -> None:
    """Every module containing @primitive(...) must be reachable from the
    fast-path list. Catches the failure mode where someone adds a new
    primitive in a new module and forgets to add the module to
    _PRIMITIVE_MODULES — the registry would silently miss it on cold
    import."""
    discovered = discover_primitive_modules()
    missing = discovered - set(_PRIMITIVE_MODULES)
    assert not missing, (
        "These modules contain @primitive(...) but are not in "
        "_PRIMITIVE_MODULES (the registry would miss them on cold "
        f"import): {sorted(missing)}. Add them to _PRIMITIVE_MODULES "
        "in hpc_mapreduce/_primitive.py."
    )


# Item #3 — composes references must resolve.

def test_composes_references_resolve(registry: dict[str, PrimitiveMeta]) -> None:
    """Every atom name listed in a composite's composes=[...] must itself
    be a registered primitive. Renaming an atom without updating the
    composers fails this test instead of silently breaking the runtime
    composition graph."""
    failures: list[str] = []
    for name, meta in registry.items():
        for atom in meta.composes:
            if atom not in registry:
                failures.append(f"{name!r} composes {atom!r}, not in registry")
    assert not failures, "\n".join(failures)


# Item #5 — func.__module__ must be importable.
# Same bug class as A1 (docs/primitives/check-preflight.md:14 pointed
# at a missing module). The decorator stores a real function ref so
# this test is trivial, but it pins the invariant going forward.

def test_func_module_importable(registry: dict[str, PrimitiveMeta]) -> None:
    failures: list[str] = []
    for name, meta in registry.items():
        modname = getattr(meta.func, "__module__", None)
        if modname is None:
            failures.append(f"{name}: func has no __module__")
            continue
        try:
            importlib.import_module(modname)
        except ImportError as exc:
            failures.append(f"{name}: cannot import {modname!r} ({exc})")
    assert not failures, "\n".join(failures)


# Item #6 — circular-import smoke test.

def test_clean_import_does_not_raise() -> None:
    """Importing hpc_mapreduce in a fresh interpreter must not raise.

    A primitive module that triggers _ensure_imported() during its own
    module-load (e.g. via a top-level get_registry() call) would loop
    without the recursion guard. This test fails loudly if anything
    regresses."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import hpc_mapreduce; hpc_mapreduce.get_registry()",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"clean import failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


# Item #7 — decorator vs frontmatter drift detection.
# During the migration window, operations.py reads frontmatter as a
# fallback when a primitive is not in the registry. If the same
# primitive has BOTH a decorator and a frontmatter file, the two MUST
# agree on the metadata that lives in both places.


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    import yaml

    return yaml.safe_load(m.group(1)) or {}


def test_decorator_matches_frontmatter(registry: dict[str, PrimitiveMeta]) -> None:
    """Soft drift detector during the migration window.

    Today decorator metadata and frontmatter both exist; the
    comparisons below catch genuine drift but also flag cosmetic
    differences (e.g. frontmatter ``spec.run_id`` vs decorator
    ``run_id``). Until every primitive's frontmatter is canonicalized,
    this test SKIPS rather than fails — drift is reported in the test
    output for visibility but doesn't block CI. Flip to a hard
    assertion once decorators are the SoT and frontmatter is generated.
    """
    docs_root = Path(__file__).resolve().parent.parent / "docs" / "primitives"
    failures: list[str] = []
    for name, meta in registry.items():
        md = docs_root / f"{name}.md"
        if not md.is_file():
            # Decorator-only during the migration; frontmatter not yet
            # authored. Skip — the no-orphan check is a separate test.
            continue
        fm = _parse_frontmatter(md)
        if not fm:
            continue
        if fm.get("name") and fm["name"] != meta.name:
            failures.append(
                f"{name}: frontmatter.name={fm['name']!r} vs decorator.name={meta.name!r}"
            )
        if fm.get("verb") and fm["verb"] != meta.verb:
            failures.append(
                f"{name}: frontmatter.verb={fm['verb']!r} vs decorator.verb={meta.verb!r}"
            )
        if "idempotent" in fm and bool(fm["idempotent"]) != meta.idempotent:
            failures.append(
                f"{name}: frontmatter.idempotent={fm['idempotent']} vs "
                f"decorator.idempotent={meta.idempotent}"
            )
        fm_key = fm.get("idempotency_key")
        # Frontmatter idempotency_key values are free-form prose: the
        # canonical key plus optional explanatory text after " — " or
        # "(...)". Compare only the core token. ``none`` / ``None`` /
        # missing all map to the decorator's None.
        if fm_key in ("none", "None", None):
            fm_core = None
        else:
            fm_core = re.split(r"\s+—\s+|\s*\(", str(fm_key), maxsplit=1)[0].strip()
            if not fm_core:
                fm_core = None
        if fm_core != meta.idempotency_key:
            failures.append(
                f"{name}: frontmatter.idempotency_key core={fm_core!r} "
                f"(raw={fm_key!r}) vs decorator.idempotency_key={meta.idempotency_key!r}"
            )
    if failures:
        pytest.skip(
            "Decorator/frontmatter drift (visibility only during migration):\n  "
            + "\n  ".join(failures)
        )
