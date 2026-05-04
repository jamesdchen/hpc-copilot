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

import contextlib
import dataclasses
import importlib
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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
    composes: tuple[PrimitiveMeta, ...] = ()
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
    composes: Iterable[Callable[..., Any] | str] | None = None,
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

    ``composes`` accepts function references to atom primitives that
    have already been decorated with ``@primitive(...)``; each callable
    must carry the decorator-attached ``_primitive_meta`` attribute.
    String names are also accepted for back-compat (looked up in the
    live ``_REGISTRY`` at decoration time). Either way, the decorator
    MUST be evaluated AFTER the atoms it references — see the ordering
    convention in ``_PRIMITIVE_MODULES``. A rename of an atom function
    becomes an import-time ``NameError`` rather than a CI test failure
    on a stale string.

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
        resolved_composes: list[PrimitiveMeta] = []
        for c in composes or ():
            if callable(c):
                ref_meta = getattr(c, "_primitive_meta", None)
                if ref_meta is None:
                    raise ValueError(
                        f"composes references {c!r} which is not a "
                        "registered primitive (no _primitive_meta "
                        "attribute — atom must be decorated before "
                        "composites that reference it)"
                    )
                resolved_composes.append(ref_meta)
            elif isinstance(c, str):
                if c not in _REGISTRY:
                    raise ValueError(
                        f"composes references {c!r} which is not a "
                        "registered primitive"
                    )
                resolved_composes.append(_REGISTRY[c])
            else:
                raise ValueError(
                    f"composes entries must be callables or names, got {c!r}"
                )
        meta = PrimitiveMeta(
            name=name,
            verb=verb,
            func=func,
            composes=tuple(resolved_composes),
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


# Modules listed here are imported on first registry query. Each
# module-level @primitive(...) call registers itself on import. Any
# new module that adds @primitive(...) MUST be appended here; a CI lint
# (``scripts/lint_primitive_modules.py``) catches drift at lint time
# without paying a runtime auto-discovery cost.
#
# ORDERING: atoms must precede the composites that reference them.
# Composite ``@primitive(composes=[atom_func, ...])`` decorators look
# up each atom_func's ``_primitive_meta`` attribute at decoration time;
# the atom decorator must have run first to attach it.
_PRIMITIVE_MODULES: tuple[str, ...] = (
    # Atoms first.
    "hpc_mapreduce.job.runs",
    "hpc_mapreduce.job.runtime_prior",
    "hpc_mapreduce.job.calibration",
    "hpc_mapreduce.job.discover",
    "hpc_mapreduce.job.resubmit",
    "hpc_mapreduce.job.planner",
    "hpc_mapreduce.job.campaign_health",
    "hpc_mapreduce.infra.inspect",
    "hpc_mapreduce.infra.clusters",
    "hpc_mapreduce.agent_cli",
    "slash_commands.runner",
    "hpc_mapreduce.job.validate",
    # Composites — must come after every atom they reference.
    "hpc_mapreduce.job.submit_flow",
    "hpc_mapreduce.job.monitor_flow",
    "hpc_mapreduce.job.aggregate_flow",
)

# Recursion guards for ``_ensure_imported``.
_IMPORTING: bool = False
_IMPORT_DONE: bool = False


def _ensure_imported() -> None:
    """Force-import every module that registers a primitive."""
    global _IMPORTING, _IMPORT_DONE
    if _IMPORT_DONE or _IMPORTING:
        return
    _IMPORTING = True
    try:
        for modname in _PRIMITIVE_MODULES:
            with contextlib.suppress(ImportError):
                importlib.import_module(modname)
    finally:
        _IMPORTING = False
        _IMPORT_DONE = True


def _reset_for_tests() -> None:
    """Test helper: clear the registry and reset the import latch."""
    global _IMPORT_DONE
    _REGISTRY.clear()
    _IMPORT_DONE = False
