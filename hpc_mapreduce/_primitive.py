"""Primitive registry — runtime catalog of all hpc-mapreduce primitives.

Implementation is SoT for behavior (the decorated function). JSON
schemas under ``hpc_mapreduce/schemas/`` are SoT for the wire contract.
``docs/primitives/*.md`` and ``operations.py``'s catalog are *views*
generated from this registry plus the schemas.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Callable, Iterable
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


# Modules listed here are imported on first registry query. Each
# module-level @primitive(...) call registers itself on import.
# Listed explicitly rather than auto-walking the package because some
# modules have side effects we don't want to trigger speculatively.
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
)


def _ensure_imported() -> None:
    """Force-import every module that registers a primitive.

    Without this, callers querying the registry before any primitive
    module has been imported would see an empty dict. A missing module
    in the list is a no-op so partial decoration during the C′ rollout
    keeps working.
    """
    for modname in _PRIMITIVE_MODULES:
        try:
            importlib.import_module(modname)
        except ImportError:
            pass


def _reset_for_tests() -> None:
    """Test helper: clear the registry. Not part of the public API."""
    _REGISTRY.clear()
