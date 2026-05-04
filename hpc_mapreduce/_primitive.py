"""Primitive registry — runtime catalog of all hpc-mapreduce primitives.

Implementation is SoT for behavior (the decorated function). JSON
schemas under ``hpc_mapreduce/schemas/`` are SoT for the wire contract.
``docs/primitives/*.md`` and ``operations.py``'s catalog are *views*
generated from this registry plus the schemas.

Decoration convention
---------------------

Most primitives have a clean Python entry point — a public function in
``hpc_mapreduce/job/`` or ``hpc_mapreduce/infra/`` that performs the
operation. Decorate that function directly with ``@primitive(...)``.

Some primitives have no inner Python helper — their behavior lives in
the ``cmd_*`` dispatcher in ``agent_cli.py`` (e.g. ``capabilities``,
``check-preflight``). For those, decorate the ``cmd_*`` function. The
registry treats both shapes uniformly; downstream consumers should not
assume ``meta.func`` is always at the primitives layer.

Migration safety
----------------

Until ``operations.py`` has fully switched off frontmatter as a
fallback source, ``docs/primitives/*.md`` and the registry are dual
sources of truth. ``tests/test_primitive_spine.py`` cross-validates
that decorator metadata matches frontmatter — drift is caught at CI
time, not silently absorbed.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import importlib.util
import pkgutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from slash_commands.errors import HpcError


VerbKind = Literal["query", "validate", "mutate", "submit", "scaffold", "workflow"]


@dataclasses.dataclass(frozen=True)
class SideEffect:
    """One declared side effect.

    ``kind``: rsync, ssh, scheduler-submit, writes-sidecar, writes-journal, ...
    ``target``: human-readable label (host, path template, ...).
    """

    kind: str
    target: str = ""


@dataclasses.dataclass(frozen=True)
class PrimitiveMeta:
    """Static metadata describing a single primitive.

    ``func`` is the canonical implementation; calling it runs the
    primitive. Every other field is metadata that other layers (the
    operations catalog, ``docs/primitives/*.md`` frontmatter, the index
    builders under ``scripts/``) used to duplicate by hand.
    """

    name: str
    verb: VerbKind
    func: Callable[..., Any]
    composes: tuple[str, ...] = ()
    side_effects: tuple[SideEffect, ...] = ()
    error_codes: tuple[type[HpcError], ...] = ()
    idempotent: bool = True
    idempotency_key: str | None = None
    exit_codes: tuple[tuple[int, str], ...] = ()
    description: str = ""


_REGISTRY: dict[str, PrimitiveMeta] = {}


def primitive(
    *,
    name: str,
    verb: VerbKind,
    composes: Iterable[str] | None = None,
    side_effects: Iterable[SideEffect] | None = None,
    error_codes: Iterable[type[HpcError]] | None = None,
    idempotent: bool = True,
    idempotency_key: str | None = None,
    exit_codes: Iterable[tuple[int, str]] | None = None,
    description: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a primitive in the runtime catalog.

    The decorated function IS the primitive's behavior. Decorator
    parameters carry the metadata that other layers (operations.py,
    primitive frontmatter, build_*_index.py) used to duplicate by hand.

    Re-registering the *same* function under the same name is a no-op
    (lets test reloads and stale __pycache__ work). Registering a
    *different* function under an existing name raises ``ValueError``.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            existing = _REGISTRY[name].func
            if existing is func:
                return func
            raise ValueError(
                f"Primitive {name!r} already registered (by {existing!r})"
            )
        meta = PrimitiveMeta(
            name=name,
            verb=verb,
            func=func,
            composes=tuple(composes or ()),
            side_effects=tuple(side_effects or ()),
            error_codes=tuple(error_codes or ()),
            idempotent=idempotent,
            idempotency_key=idempotency_key,
            exit_codes=tuple(exit_codes or ()),
            description=(
                description
                or (func.__doc__ or "").strip().split("\n", 1)[0]
            ),
        )
        _REGISTRY[name] = meta
        func._primitive_meta = meta  # type: ignore[attr-defined]
        return func

    return decorator


def get_registry() -> dict[str, PrimitiveMeta]:
    """Return a snapshot of the registry, importing primitive-bearing modules first."""
    _ensure_imported()
    return dict(_REGISTRY)


def get_meta(name: str) -> PrimitiveMeta:
    """Return the :class:`PrimitiveMeta` for ``name`` (KeyError if absent)."""
    _ensure_imported()
    return _REGISTRY[name]


# Modules listed here are imported on first registry query (fast path
# — avoids walking the filesystem on every call). Each module-level
# @primitive(...) call registers itself on import. The ``discover_*``
# helper below is the safety net that catches any module added without
# updating this list (item #2 of the C′ breakage list — "registered
# but invisible").
_PRIMITIVE_MODULES: tuple[str, ...] = (
    "hpc_mapreduce.job.submit_flow",
    "hpc_mapreduce.job.monitor_flow",
    "hpc_mapreduce.job.aggregate_flow",
    "hpc_mapreduce.job.runs",
    "hpc_mapreduce.job.blacklist",
    "hpc_mapreduce.job.runtime_prior",
    "hpc_mapreduce.job.calibration",
    "hpc_mapreduce.job.discover",
    "hpc_mapreduce.job.resubmit",
    "hpc_mapreduce.job.planner",
    "hpc_mapreduce.infra.inspect",
    "hpc_mapreduce.infra.clusters",
    "hpc_mapreduce.agent_cli",
    "slash_commands.runner",
    "hpc_mapreduce.job.validate",
)

# Recursion guards for ``_ensure_imported`` (item #6 of the C′
# breakage list — "circular import risk"). A primitive module's
# top-level @primitive(...) call decorates a function whose definition
# can transitively trigger imports. If any of those imports re-enters
# ``get_registry()`` while we are mid-import, ``_ensure_imported``
# would loop. ``_IMPORTING`` short-circuits the inner call so the
# outermost frame completes the import sequence atomically;
# ``_IMPORT_DONE`` makes subsequent calls cheap.
_IMPORTING: bool = False
_IMPORT_DONE: bool = False


def discover_primitive_modules(
    roots: Iterable[str] = ("hpc_mapreduce", "slash_commands"),
) -> set[str]:
    """Return module names whose source contains ``@primitive(...)``.

    Uses ``ast`` to parse module sources without importing them, so
    discovery is side-effect-free. The CI cross-validation test asserts
    this set is a subset of ``_PRIMITIVE_MODULES`` so no orphans slip
    past the fast-path list.
    """
    found: set[str] = set()
    for root in roots:
        try:
            pkg = importlib.import_module(root)
        except ImportError:
            continue
        if not hasattr(pkg, "__path__"):
            continue
        for info in pkgutil.walk_packages(pkg.__path__, prefix=root + "."):
            modname = info.name
            try:
                spec = importlib.util.find_spec(modname)
            except (ImportError, AttributeError, ValueError):
                continue
            if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
                continue
            try:
                source = Path(spec.origin).read_text(encoding="utf-8")
            except OSError:
                continue
            if "@primitive" not in source:
                continue
            try:
                tree = ast.parse(source, filename=spec.origin)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for dec in node.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    name = getattr(target, "id", None) or getattr(target, "attr", None)
                    if name == "primitive":
                        found.add(modname)
                        break
                if modname in found:
                    break
    return found


def _ensure_imported() -> None:
    """Force-import every module that registers a primitive.

    Fast path: import the explicit ``_PRIMITIVE_MODULES`` list. Safety
    net: on first call only, also walk the discovered module set to
    catch anything missed. Subsequent calls short-circuit on
    ``_IMPORT_DONE``. The ``_IMPORTING`` guard prevents recursion if
    decorated code triggers ``get_registry()`` during its own
    module-load.
    """
    global _IMPORTING, _IMPORT_DONE
    if _IMPORT_DONE or _IMPORTING:
        return
    _IMPORTING = True
    try:
        for modname in _PRIMITIVE_MODULES:
            try:
                importlib.import_module(modname)
            except ImportError:
                pass
        try:
            for modname in discover_primitive_modules():
                if modname in _PRIMITIVE_MODULES:
                    continue
                try:
                    importlib.import_module(modname)
                except ImportError:
                    pass
        except Exception:
            # Auto-discovery is best-effort. Never let it break callers
            # if the file-system layout is unusual (read-only install,
            # namespace packages, frozen importer, etc.).
            pass
    finally:
        _IMPORTING = False
        _IMPORT_DONE = True


def _reset_for_tests() -> None:
    """Test helper: clear the registry and reset the import latch.

    Not part of the public API. Tests that need a clean registry
    snapshot call this before re-importing primitive modules.
    """
    global _IMPORT_DONE
    _REGISTRY.clear()
    _IMPORT_DONE = False
