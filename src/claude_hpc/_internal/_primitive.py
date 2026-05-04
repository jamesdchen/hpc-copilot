"""Primitive registry — runtime catalog of all hpc-mapreduce primitives.

Implementation is SoT for behavior (the decorated function). JSON
schemas under ``claude_hpc/schemas/`` are SoT for the wire contract.
``docs/primitives/*.md`` and ``operations.py``'s catalog are *views*
generated from this registry plus the schemas.

Decoration convention
---------------------

Most primitives have a clean Python entry point — a public function in
``claude_hpc/orchestrator/`` or ``claude_hpc/forecast/`` or ``claude_hpc/infra/`` that performs the
operation. Decorate that function directly with ``@primitive(...)``.

Some primitives have no inner Python helper — their behavior lives in
the ``cmd_*`` dispatcher in ``agent_cli.py`` (e.g. ``capabilities``,
``check-preflight``). For those, decorate the ``cmd_*`` function. The
registry treats both shapes uniformly; downstream consumers should not
assume ``meta.func`` is always at the primitives layer.

Population
----------

Callers MUST invoke :func:`register_primitives` once at process startup
before querying :func:`get_registry` / :func:`get_meta`. Querying the
registry before registration raises ``RuntimeError`` — the previous
auto-import-on-first-query behaviour silently swallowed
``ImportError`` and made hard-to-diagnose missing-decorator bugs.
``register_primitives`` itself is idempotent.

Migration safety
----------------

Until ``operations.py`` has fully switched off frontmatter as a
fallback source, ``docs/primitives/*.md`` and the registry are dual
sources of truth. ``tests/test_primitive_spine.py`` cross-validates
that decorator metadata matches frontmatter — drift is caught at CI
time, not silently absorbed.
"""

from __future__ import annotations

import dataclasses
import importlib
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from claude_hpc.errors import HpcError


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
_REGISTRATION_DONE: bool = False


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
            raise ValueError(f"Primitive {name!r} already registered (by {existing!r})")
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
                        f"composes references {c!r} which is not a registered primitive"
                    )
                resolved_composes.append(_REGISTRY[c])
            else:
                raise ValueError(f"composes entries must be callables or names, got {c!r}")
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
            description=(description or (func.__doc__ or "").strip().split("\n", 1)[0]),
        )
        _REGISTRY[name] = meta
        func._primitive_meta = meta  # type: ignore[attr-defined]
        return func

    return decorator


# Modules listed here are imported once by :func:`register_primitives`.
# Each module-level @primitive(...) call registers itself on import.
# Any new module that adds @primitive(...) MUST be appended here; a CI
# lint (``scripts/lint_primitive_modules.py``) catches drift at lint
# time without paying any runtime cost.
#
# ORDERING: atoms must precede the composites that reference them.
# Composite ``@primitive(composes=[atom_func, ...])`` decorators look
# up each atom_func's ``_primitive_meta`` attribute at decoration time;
# the atom decorator must have run first to attach it.
_PRIMITIVE_MODULES: tuple[str, ...] = (
    # Atoms first.
    "claude_hpc.orchestrator.runs",
    "claude_hpc.orchestrator.runtime_prior",
    "claude_hpc.orchestrator.calibration",
    "claude_hpc.orchestrator.discover",
    "claude_hpc.orchestrator.resubmit",
    "claude_hpc.orchestrator.planner",
    "claude_hpc.orchestrator.campaign_health",
    "claude_hpc.infra.inspect",
    "claude_hpc.infra.clusters",
    "claude_hpc.agent_cli",
    "claude_hpc.atoms.campaign_list",
    "claude_hpc.atoms.campaign_status",
    "claude_hpc.atoms.capabilities",
    "claude_hpc.atoms.clusters",
    "claude_hpc.atoms.failures",
    "claude_hpc.atoms.house_edge",
    "claude_hpc.atoms.list_in_flight",
    "claude_hpc.atoms.logs",
    "claude_hpc.atoms.preflight",
    "claude_hpc.atoms.walltime_drift",
    "slash_commands.runner",
    "claude_hpc.orchestrator.validate",
    # Composites — must come after every atom they reference.
    "claude_hpc.orchestrator.submit_flow",
    "claude_hpc.orchestrator.monitor_flow",
    "claude_hpc.orchestrator.aggregate_flow",
)


def register_primitives() -> None:
    """Import every module that decorates a primitive.

    Must be called once before the registry is queried. Idempotent —
    re-calling is a no-op. Modules in ``_PRIMITIVE_MODULES`` are imported
    in order; the import side-effect of each module's @primitive(...)
    calls populates ``_REGISTRY``.

    Atoms must precede the composites that reference them in the list
    because composites' @primitive(composes=[atom_func, ...]) decorators
    look up the atom's ``_primitive_meta`` attribute at decoration time.

    Unlike the previous auto-import path this function does NOT
    silently swallow ``ImportError`` — a bad primitive module fails the
    call loudly so the missing decorator surfaces immediately rather
    than as a downstream "primitive not in registry" error.
    """
    global _REGISTRATION_DONE
    if _REGISTRATION_DONE:
        return
    for modname in _PRIMITIVE_MODULES:
        importlib.import_module(modname)
    _REGISTRATION_DONE = True


def get_registry() -> dict[str, PrimitiveMeta]:
    """Return a snapshot of the primitive registry.

    Raises ``RuntimeError`` if :func:`register_primitives` has not yet
    been called. Tests configure an autouse session fixture; the
    ``hpc-mapreduce`` CLI invokes it from ``main()`` before subcommand
    dispatch.
    """
    if not _REGISTRATION_DONE:
        raise RuntimeError(
            "Primitive registry queried before register_primitives() "
            "was called. Call claude_hpc.register_primitives() at "
            "process startup."
        )
    return dict(_REGISTRY)


def get_meta(name: str) -> PrimitiveMeta:
    """Return the :class:`PrimitiveMeta` for ``name`` (KeyError if absent)."""
    if not _REGISTRATION_DONE:
        raise RuntimeError(
            "Primitive registry queried before register_primitives() "
            "was called. Call claude_hpc.register_primitives() at "
            "process startup."
        )
    return _REGISTRY[name]


def _reset_for_tests() -> None:
    """Test helper: clear the registry and reset the registration latch.

    Not part of the public API. Tests that need a clean registry
    snapshot call this before re-importing primitive modules.
    """
    global _REGISTRATION_DONE
    _REGISTRY.clear()
    _REGISTRATION_DONE = False
