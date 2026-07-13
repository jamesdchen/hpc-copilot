"""CI lint: every public ``_wire`` model reaches an emitted schema (or is a declared helper).

Schema emission (``scripts/build_schemas.py``) dispatches on a model's *class-name
suffix*. ``*Spec`` / ``*Input`` become ``<snake>.input.json``; ``*Result`` /
``*Report`` / ``*Envelope`` become ``<snake>.output.json``; **anything else is
silently skipped as a helper**. That silence hides two failure modes:

1. **A third suffix emits nothing.** A model authored as wire I/O but named with a
   suffix outside ``build_schemas._SUFFIX_RULES`` (e.g. ``FooRequest`` instead of
   ``FooSpec``) is treated as a helper and produces no schema — no error, no file.
   The primitive ships with no contract and nobody notices until a consumer breaks.
2. **A rename degrades ``schema_for()`` to ``None``.** ``schema_for``
   (``registry/operations.py``) and ``_output_schema_for`` (``contract/schema.py``)
   resolve a schema *file* by name convention and return ``None`` when it's absent.
   If a model resolves to ``foo.input.json`` but that file was renamed / deleted,
   both silently degrade to "no schema" instead of failing.

This lint converts both into loud CI failures. It walks ``hpc_agent._wire`` and,
reusing ``build_schemas``' own discovery as the single source of truth, asserts:

* **Reachability** — every public ``BaseModel`` whose name ends in a *registered
  emitting suffix* either resolves to an emitted schema **or** is an explicit
  helper in ``build_schemas._HELPER_NAMES`` (e.g. ``MpiSpec``, ``SuccessEnvelope``).
  A ``*Spec`` model that quietly stops emitting fails here.
* **No I/O-shaped third suffix** — a public model ending in a suffix that *looks*
  like wire I/O but is **not** a registered emitting suffix (``*Request``,
  ``*Response``, ``*Output``, ...) must resolve or be in ``_RESERVED_SUFFIX_HELPERS``.
  A new ``FooRequest`` fails until it's renamed to a registered suffix or declared a
  helper — the "third suffix" trap made loud.
* **On-disk existence** — every model ``build_schemas`` resolves must have its
  emitted schema file present on disk. A rename that orphans the file fires here
  rather than in a silent ``schema_for() -> None``.

Models with a plain non-I/O suffix (``*Line``, ``*Entry``, ``*Record``, ``*Row``,
...) are embedded sub-models by convention and are out of scope — they never emit a
standalone schema and don't invite the "I meant this to be wire I/O" mistake.

``build_schemas`` is imported (not ``ast``-parsed) so the suffix rules, helper set,
and registry stay a single source of truth: this lint can never disagree with the
generator about what a suffix means.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from pydantic import BaseModel  # noqa: E402

# Suffixes that *look* like wire I/O but are NOT in build_schemas._SUFFIX_RULES.
# A public model ending in one of these is almost certainly an author who expected
# a schema and won't get one (the "third suffix" trap). ``Config`` is deliberately
# excluded — it's a common helper suffix (``AxesConfig`` already resolves via the
# non-suffix mapping) and would only add noise.
_RESERVED_ALIAS_SUFFIXES: tuple[str, ...] = (
    "Request",
    "Response",
    "Output",
    "Payload",
    "Params",
    "Command",
    "Query",
    "Body",
    "Dto",
    "Args",
)

# Public models that legitimately end in an I/O-shaped alias suffix yet ship no
# standalone schema. Each is an in-process contract or an embedded sub-model, not a
# primitive input/output. Additions here are the deliberate, reviewed "yes, this is
# a helper" act the lint exists to force.
_RESERVED_SUFFIX_HELPERS: frozenset[str] = frozenset(
    {
        # spawn_contract.SpawnRequest — the request the orchestrator hands the
        # worker spawner; validated in-process, no CLI verb, no emitted schema.
        "SpawnRequest",
        # notebook_lint.DeclaredOutput — embedded sub-model of the lint report.
        "DeclaredOutput",
    }
)


def _load_build_schemas() -> ModuleType:
    """Import ``scripts/build_schemas.py`` as a module (reuse its discovery + registry).

    Loaded by path so the suffix rules, helper names, and SCHEMA_REGISTRY come from
    exactly the generator that emits the schemas — one source of truth, no drift.
    """
    spec = importlib.util.spec_from_file_location(
        "_build_schemas_for_wire_lint", REPO / "scripts" / "build_schemas.py"
    )
    assert spec is not None and spec.loader is not None
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def _iter_public_wire_models() -> Iterable[tuple[type[BaseModel], str]]:
    """Yield ``(model, module_name)`` for every public ``BaseModel`` defined in a
    non-private ``hpc_agent._wire`` submodule.

    Mirrors ``build_schemas._build_schema_registry_for``'s walk: recurse with
    ``walk_packages``, skip ``_``-prefixed leaf modules and attributes, and count a
    class only in the module that *defines* it (re-imports are skipped via the
    ``__module__`` check) so a class isn't reported twice.
    """
    import hpc_agent._wire as wire

    for _finder, modname, _ispkg in pkgutil.walk_packages(
        wire.__path__, prefix=f"{wire.__name__}."
    ):
        leaf = modname.rsplit(".", 1)[-1]
        if leaf.startswith("_"):
            continue
        mod = importlib.import_module(modname)
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if not (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel):
                continue
            if getattr(obj, "__module__", None) != mod.__name__:
                continue
            yield obj, modname


def find_violations(
    models: Iterable[tuple[type[BaseModel], str]],
    bs: ModuleType,
) -> list[str]:
    """Return a list of violation strings (empty == clean).

    *bs* is the imported ``build_schemas`` module; *models* is an iterable of
    ``(model, module_name)`` pairs (injectable so a test can plant a synthetic
    mis-suffixed model). Three checks: reachability of registered-suffix models,
    the I/O-shaped third-suffix trap, and on-disk existence of every resolved file.
    """
    registered_suffixes = tuple(suffix for suffix, _side in bs._SUFFIX_RULES)

    # Models build_schemas resolves to a schema, keyed by class object.
    resolved: dict[type, str] = {}
    for src, fname, _schemas_dir in bs.SCHEMA_REGISTRY:
        if isinstance(src, type):
            resolved[src] = fname

    violations: list[str] = []

    # 1 + 2: per-model reachability / third-suffix trap.
    for model, modname in models:
        if model in resolved:
            continue  # emits a schema — file existence is checked below.
        name = model.__name__
        reg = next((s for s in registered_suffixes if name.endswith(s)), None)
        if reg is not None:
            if name in bs._HELPER_NAMES:
                continue
            violations.append(
                f"{modname}.{name}: ends in registered emitting suffix {reg!r} but "
                "build_schemas produces no schema for it. Either it must emit "
                "(check build_schemas discovery / that it's defined in its module) "
                "or add it to build_schemas._HELPER_NAMES as an explicit helper."
            )
            continue
        alias = next((s for s in _RESERVED_ALIAS_SUFFIXES if name.endswith(s)), None)
        if alias is not None and name not in _RESERVED_SUFFIX_HELPERS:
            registered = ", ".join(registered_suffixes)
            violations.append(
                f"{modname}.{name}: ends in I/O-shaped suffix {alias!r}, which is NOT "
                f"a registered emitting suffix ({registered}) — a model with this "
                "suffix silently emits no schema. Rename it to a registered suffix, "
                "or add it to lint_wire_suffix._RESERVED_SUFFIX_HELPERS if it is "
                "genuinely an embedded/in-process helper."
            )

    # 3: every resolved model's schema file must exist on disk (a rename that
    # orphans the file otherwise degrades schema_for()/_output_schema_for() to None).
    for src, fname, schemas_dir in bs.SCHEMA_REGISTRY:
        if not (schemas_dir / fname).is_file():
            label = src.__name__ if isinstance(src, type) else repr(src)
            violations.append(
                f"{label}: resolves to schema file {fname!r} but it is missing under "
                f"{schemas_dir} — schema_for()/_output_schema_for() would degrade to "
                "None. Run scripts/build_schemas.py --write."
            )

    return violations


def main() -> int:
    bs = _load_build_schemas()
    violations = find_violations(_iter_public_wire_models(), bs)
    if violations:
        print("ERROR: _wire models that don't resolve to an emitted schema:")
        for v in violations:
            print(f"  {v}")
        print(
            "\nWhy: build_schemas emits a schema only for models whose class name ends "
            "in a registered suffix (Spec/Input/Result/Report/Envelope). A model with "
            "any other suffix is silently skipped, and a renamed/missing schema file "
            "makes schema_for() degrade to None. Keep every public wire model either "
            "reachable (emits a schema) or an explicit helper."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
