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

from claude_hpc._internal._primitive import (
    PrimitiveMeta,
    get_registry,
)


@pytest.fixture(scope="module")
def registry() -> dict[str, PrimitiveMeta]:
    return get_registry()


# Item #2 — orphan-module detection moved to a CI lint
# (``scripts/lint_primitive_modules.py``). The lint catches the
# failure mode where someone adds a new primitive in a new module and
# forgets to add the module to ``_PRIMITIVE_MODULES`` — the registry
# would silently miss it on cold import. ``test_lint_primitive_modules``
# subprocess-invokes the script so test runs catch drift even without
# CI.


# Item #3 — composes references must resolve.

def test_composes_references_resolve(registry: dict[str, PrimitiveMeta]) -> None:
    """Every entry in ``meta.composes`` must be a :class:`PrimitiveMeta`
    that resolves back to a registry entry by both name and identity.

    The decorator now stores function references (resolved at decoration
    time via the atom's ``_primitive_meta`` attribute), so the
    composition graph is wired with real Python identities — a renamed
    atom is an import-time NameError. This test additionally pins the
    invariant that no stale or shadow ``PrimitiveMeta`` snuck in: the
    object referenced from the composite's ``composes`` tuple must be
    THE registry entry's meta, and the atom's underlying ``func`` must
    be the same callable currently registered under that name.
    """
    failures: list[str] = []
    for name, meta in registry.items():
        for atom_meta in meta.composes:
            if not isinstance(atom_meta, PrimitiveMeta):
                failures.append(
                    f"{name!r} composes entry {atom_meta!r} is not a PrimitiveMeta"
                )
                continue
            registered = registry.get(atom_meta.name)
            if registered is None:
                failures.append(
                    f"{name!r} composes {atom_meta.name!r}, not in registry"
                )
                continue
            if registered.func is not atom_meta.func:
                failures.append(
                    f"{name!r} composes {atom_meta.name!r} but the referenced "
                    "function is not the registered one"
                )
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

def test_register_primitives_is_idempotent() -> None:
    """Calling register_primitives() twice must be a no-op.

    The new explicit path replaces the previous _ensure_imported()
    auto-import on first registry query. Idempotency guarantees that
    any double-call (e.g. main() -> register_primitives() followed by a
    test fixture also calling it) doesn't re-register and trip the
    "primitive already registered" guard.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import hpc_mapreduce as hp\n"
            "hp.register_primitives()\n"
            "first = dict(hp.get_registry())\n"
            "hp.register_primitives()\n"
            "second = dict(hp.get_registry())\n"
            "assert first.keys() == second.keys(), (first, second)\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"register_primitives idempotency check failed:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
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
    """Decorator metadata and frontmatter MUST agree exactly.

    The registry is now the single source of truth and
    ``scripts/build_primitive_frontmatter.py`` regenerates the YAML
    block from it. Drift is a real bug; the CI gate runs ``--check``
    on every PR so a developer who edits a primitive decorator without
    regenerating the frontmatter has to fix the gap before merging.
    """
    docs_root = Path(__file__).resolve().parent.parent / "docs" / "primitives"
    failures: list[str] = []
    for name, meta in registry.items():
        md = docs_root / f"{name}.md"
        if not md.is_file():
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
        if fm_key in ("none", "None", None):
            fm_core: str | None = None
        else:
            fm_core = str(fm_key)
        if fm_core != meta.idempotency_key:
            failures.append(
                f"{name}: frontmatter.idempotency_key={fm_key!r} "
                f"vs decorator.idempotency_key={meta.idempotency_key!r}"
            )
    assert not failures, (
        "Decorator/frontmatter drift — run "
        "``python scripts/build_primitive_frontmatter.py --write`` "
        "to regenerate:\n  "
        + "\n  ".join(failures)
    )



# ---------------------------------------------------------------------------
# Additional invariants from the C′ design (one block per item).
# ---------------------------------------------------------------------------


def test_error_codes_subclass_hpc_error(registry: dict[str, PrimitiveMeta]) -> None:
    """Every class in ``meta.error_codes`` must be an :class:`HpcError`.

    The decorator types ``error_codes`` as ``tuple[type[HpcError], ...]``,
    so this is belt-and-suspenders: it catches the case where someone
    passes a non-HpcError class (e.g. ValueError) to the decorator, which
    typing tools won't catch at all call sites.
    """
    from slash_commands.errors import HpcError

    failures: list[str] = []
    for name, meta in registry.items():
        for cls in meta.error_codes:
            if not isinstance(cls, type) or not issubclass(cls, HpcError):
                failures.append(f"{name}: error_codes contains {cls!r}, not an HpcError subclass")
    assert not failures, "\n".join(failures)


def test_workflow_primitives_compose_at_least_one_atom(
    registry: dict[str, PrimitiveMeta],
) -> None:
    """A ``workflow`` primitive's whole purpose is to chain atoms.

    Asserting at least one composes entry resolves to an atom in the
    registry catches the case where a workflow's composes list is
    empty (lost during a refactor) or refers exclusively to atoms that
    no longer exist (caught more strictly by
    ``test_composes_references_resolve``, but the empty-list case is
    its own failure mode).
    """
    failures: list[str] = []
    for name, meta in registry.items():
        if meta.verb != "workflow":
            continue
        if not meta.composes:
            failures.append(f"{name}: workflow primitive declares no composes")
            continue
        resolved = [a for a in meta.composes if a.name in registry]
        if not resolved:
            failures.append(
                f"{name}: workflow primitive's composes={list(meta.composes)} "
                "does not resolve to any registered atom"
            )
    assert not failures, "\n".join(failures)


def test_idempotency_key_names_input_schema_property(
    registry: dict[str, PrimitiveMeta],
) -> None:
    """If ``idempotency_key`` is set, it must be a property on the input schema.

    Catches drift between the decorator's claim ("dedup on field X")
    and the wire contract ("input schema doesn't have field X"), which
    would otherwise show up as a runtime KeyError on the first replay.

    Compound keys like ``"(cluster, node)"`` are tolerated: their
    constituent token list is checked against the input schema's
    ``properties``. Dotted keys like ``"spec.run_id"`` are split on
    the dot — the head must be a top-level property; we don't recurse
    further (input schemas often don't model nested specs in detail).
    """
    import json
    import re

    schemas_dir = Path(__file__).resolve().parent.parent / "hpc_mapreduce" / "schemas"

    def _input_schema_for(name: str) -> dict | None:
        for fname in (
            f"{name.replace('-', '_')}.input.json",
            f"{name}.input.json",
        ):
            path = schemas_dir / fname
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return None
        return None

    failures: list[str] = []
    for name, meta in registry.items():
        if meta.idempotency_key is None:
            continue
        schema = _input_schema_for(name)
        if schema is None:
            # Many Python-only primitives have no input schema. The
            # idempotency_key invariant only applies to wire contracts,
            # so skip when there's nothing to check.
            continue
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            continue
        # Tokenize the key. Tuple-style "(cluster, node)" -> ["cluster", "node"].
        # Dotted "spec.run_id" -> ["spec"]. Plain "run_id" -> ["run_id"].
        raw = meta.idempotency_key
        tuple_match = re.match(r"^\(\s*(.+)\s*\)$", raw)
        if tuple_match:
            tokens = [t.strip() for t in tuple_match.group(1).split(",") if t.strip()]
        elif "." in raw:
            tokens = [raw.split(".", 1)[0]]
        else:
            tokens = [raw]
        for tok in tokens:
            if tok not in properties:
                failures.append(
                    f"{name}: idempotency_key={meta.idempotency_key!r} "
                    f"references {tok!r} which is not a property of the input schema "
                    f"(properties: {sorted(properties)})"
                )
    assert not failures, "\n".join(failures)
