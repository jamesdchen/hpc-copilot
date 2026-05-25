"""Primitive registry — runtime catalog of all hpc-agent primitives.

Implementation is SoT for behavior (the decorated function). JSON
schemas under ``hpc_agent/schemas/`` are SoT for the wire contract.
``docs/primitives/*.md`` and ``operations.py``'s catalog are *views*
generated from this registry plus the schemas.

Decoration convention
---------------------

Most primitives have a clean Python entry point — a public function in one
of the engine subpackages (``runner/``, ``planning/``, ``state/``,
``flows/``, or ``infra/``) that performs the operation.
Decorate that function directly with ``@primitive(...)``.

Some primitives have no inner Python helper — their behavior lives in
the ``cmd_*`` dispatchers under ``hpc_agent/cli/`` (e.g.
``capabilities``, ``check-preflight``). For those, decorate the
``cmd_*`` function. The
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

Source-of-truth split
---------------------

The registry IS the canonical source for the structured metadata
the decorator carries: ``name``, ``verb``, ``side_effects``,
``idempotent``, ``idempotency_key``, ``error_codes``, ``composes``,
and ``cli`` (the shell invocation string).
:func:`hpc_agent._kernel.registry.operations.operations_catalog` reads the
registry directly; nothing else reads the markdown frontmatter for
those fields.

``docs/primitives/<name>.md`` carries two kinds of content:

1. **Registry-derived frontmatter** — auto-rewritten by
   ``scripts/build_primitive_frontmatter.py`` from the registry. Never
   hand-edit; the pre-commit hook + the CI ``--check`` gate will undo
   you.
2. **Hand-authored prose** — everything after the closing ``---``
   marker, plus the ``inputs:`` / ``outputs:`` / ``backed_by:`` /
   ``exit_codes:`` frontmatter fields the registry doesn't yet model.
   These are round-tripped untouched.

Primitives missing a ``docs/primitives/<name>.md`` are auto-scaffolded
with a one-line placeholder by the regen script, so the registry can't
silently sprout undocumented primitives.
"""

from __future__ import annotations

import dataclasses
import importlib
from typing import TYPE_CHECKING, Any, Literal, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from hpc_agent.cli._dispatch import CliShape
    from hpc_agent.errors import HpcError


VerbKind = Literal["query", "validate", "mutate", "submit", "scaffold", "workflow"]

# Preserve the decorated function's signature so mypy sees the original
# return type at call sites (the decorator returns the func unchanged).
F = TypeVar("F", bound="Callable[..., Any]")


@dataclasses.dataclass(frozen=True)
class SideEffect:
    """One declared side effect.

    ``kind``: sync-push, sync-pull, ssh, scheduler-submit, writes-sidecar,
    writes-journal, ... The ``sync-*`` kinds are transport-agnostic: the
    runtime uses rsync when available and falls back to a ``tar c | ssh
    tar x`` push / ``scp -r`` pull pipeline otherwise (see infra.remote).
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
    # CLI declaration — a :class:`hpc_agent.cli._dispatch.CliShape`
    # consumed by :func:`hpc_agent.cli._dispatch.dispatch_primitive`.
    # ``None`` marks a primitive with no standalone shell verb
    # (composed into other primitives, or a Tier 3 verb whose adapter
    # lives in ``cli/<module>.py`` with its own ``register(sub)``).
    cli: CliShape | None = None
    # Whether the LLM/agent calls this primitive directly. Workflows,
    # scaffolds, validators, and atoms slash-commands or skills link to
    # are ``True``; framework internals composed inside workflows
    # (e.g. ``poll-run-status`` inside ``monitor-flow``) default to
    # ``False``. Read by :func:`render_llms_full` to decide which
    # primitives ship their full body + schemas in the agent context
    # dump vs. only appearing as a row in the catalog table. The
    # catalog table itself is always full so an agent can still
    # introspect "what exists" and shell to a CLI form for forensic
    # access; tiering only applies to the per-primitive prose + schema
    # block in the ``llms-full`` blob.
    agent_facing: bool = False


_REGISTRY: dict[str, PrimitiveMeta] = {}
_REGISTRATION_DONE: bool = False

# Pending string-name composes waiting for lazy resolution. Populated by
# the decorator; drained by :func:`_finalize_composes` after every
# primitive-bearing module has been imported. Lets composers reference
# atoms by their wire name without forcing an atoms-before-composites
# import order — the registry is the single source of truth for "name →
# meta", so resolution can happen at the natural boundary (the end of
# :func:`register_primitives`) instead of at decoration time.
_PENDING_COMPOSES: dict[str, tuple[str, ...]] = {}


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
    cli: CliShape | None = None,
    agent_facing: bool = False,
) -> Callable[[F], F]:
    """Register a primitive in the runtime catalog.

    The decorated function IS the primitive's behavior. Decorator
    parameters carry the metadata that other layers (operations.py,
    primitive frontmatter, build_*_index.py) used to duplicate by hand.

    ``composes`` accepts either:

    * **Callable references** to atom primitives that have already been
      decorated. Each callable must carry the decorator-attached
      ``_primitive_meta`` attribute — i.e. the atom decorator must have
      run first. The composer's module must therefore ``import`` the
      atom's module before its own decorator fires. Mostly used by
      same-subject composition and plugin compose-into-core.

    * **String names** — the primitive's wire name. Stashed in
      ``_PENDING_COMPOSES`` and resolved lazily by
      :func:`_finalize_composes` after every primitive-bearing module
      has been imported. String-name composes are order-agnostic at the
      module-import layer: an atom and its composer can be discovered in
      any order; the registry IS the single source of truth for
      ``name → meta``, and resolution happens once at that boundary.

    Re-registering the *same* function under the same name is a no-op
    (lets test reloads and stale __pycache__ work). Registering a
    *different* function under an existing name raises ``ValueError``.
    """

    def decorator(func: F) -> F:
        if name in _REGISTRY:
            existing = _REGISTRY[name].func
            if existing is func:
                return func
            raise ValueError(f"Primitive {name!r} already registered (by {existing!r})")
        # Idempotency-key gate: a state-mutating primitive that claims
        # idempotent=True owes the caller an equivalence rule. Without
        # one, the registry says "safe to retry" but doesn't say what
        # makes two calls equivalent — which is exactly what idempotent
        # means. Enforce on verbs that touch state (mutate, submit,
        # workflow, scaffold). Pure-query/validate primitives are
        # observation-only; their idempotency comes for free.
        _STATEFUL_VERBS = ("mutate", "submit", "workflow", "scaffold")
        if (
            idempotent
            and idempotency_key is None
            and verb in _STATEFUL_VERBS
            and (side_effects or ())
        ):
            raise ValueError(
                f"Primitive {name!r} (verb={verb!r}, has side_effects) "
                f"declares idempotent=True but no idempotency_key. "
                "State-mutating primitives must declare what makes two "
                "calls equivalent — typically the natural identifier "
                "argument (e.g. 'run_id', 'experiment_dir', 'campaign_id'). "
                "Either pass idempotency_key=, or set idempotent=False if "
                "retries genuinely aren't safe."
            )
        resolved_composes: list[PrimitiveMeta] = []
        pending_names: list[str] = []
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
                # Lazy: don't look up against ``_REGISTRY`` here. Stash
                # the name; :func:`_finalize_composes` resolves it after
                # every primitive-bearing module has been imported. That
                # eliminates the atoms-before-composites ordering
                # requirement on the module-import sequence — composes=
                # by name is now order-agnostic.
                pending_names.append(c)
            else:
                raise ValueError(f"composes entries must be callables or names, got {c!r}")
        if pending_names:
            _PENDING_COMPOSES[name] = tuple(pending_names)
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
            cli=cli,
            agent_facing=agent_facing,
        )
        _REGISTRY[name] = meta
        func._primitive_meta = meta  # type: ignore[attr-defined]
        return func

    return decorator


# Top-level packages walked by :func:`register_primitives` for primitive
# discovery. Every public ``.py`` module under one of these roots is
# imported; any ``@primitive(...)`` decorator at module-import time
# registers itself in ``_REGISTRY``. Modules with no decorator are
# import-no-ops (cheap; Python caches the module).
#
# Replaces a hand-maintained ``_PRIMITIVE_MODULES`` tuple + a separate
# ``scripts/lint_primitive_modules.py`` drift-detector. The package walk
# is the SoT now; nothing to drift against.
#
# To add a primitive to a NEW top-level package, append the package name
# here. (Keep this small — packages outside this list don't get scanned.)
_PRIMITIVE_PACKAGES: tuple[str, ...] = (
    "hpc_agent.ops",
    "hpc_agent.meta",
    "hpc_agent.incorporation",
    "hpc_agent.state",
    "hpc_agent.cli",
    "hpc_agent._kernel.extension",
)


def _discover_primitive_modules() -> list[str]:
    """Yield every importable submodule under :data:`_PRIMITIVE_PACKAGES`.

    Uses :func:`pkgutil.walk_packages` so subpackages (e.g.
    ``ops/submit/``) are recursed into. Order is deterministic but
    irrelevant — :func:`_finalize_composes` resolves string-name
    ``composes=`` after every module has been imported.
    """
    import pkgutil

    out: list[str] = []
    for pkg_name in _PRIMITIVE_PACKAGES:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            # Mainline packages are always present; tolerate absence
            # only for forward-compat with pruned distributions.
            continue
        pkg_path = getattr(pkg, "__path__", None)
        if pkg_path is None:
            continue
        for _finder, name, _ispkg in pkgutil.walk_packages(pkg_path, prefix=pkg_name + "."):
            # Skip dunder / private modules; their decoration sites (if
            # any) are intentionally not part of the surface.
            leaf = name.rsplit(".", 1)[-1]
            if leaf.startswith("_") and not leaf.startswith("__"):
                continue
            out.append(name)
    return out


def _finalize_composes() -> None:
    """Resolve every pending string-name ``composes=`` against ``_REGISTRY``.

    Called once at the end of :func:`register_primitives` after every
    primitive-bearing module has been imported. Replaces each affected
    :class:`PrimitiveMeta` (frozen) with a new instance carrying the
    fully-resolved ``composes`` tuple, and rebinds the func's
    ``_primitive_meta`` attribute so callable refs stay consistent.

    Raises ``ValueError`` listing every unresolved string at once so a
    typo surfaces all its sibling references in one error rather than
    a fail-fast death-march.
    """
    if not _PENDING_COMPOSES:
        return
    unresolved: list[str] = []
    for prim_name, pending in list(_PENDING_COMPOSES.items()):
        existing = _REGISTRY.get(prim_name)
        if existing is None:
            unresolved.append(f"{prim_name}: pending composes but primitive missing from registry")
            continue
        extra: list[PrimitiveMeta] = []
        for ref in pending:
            target = _REGISTRY.get(ref)
            if target is None:
                unresolved.append(
                    f"{prim_name}: composes references {ref!r}, not a registered primitive"
                )
                continue
            extra.append(target)
        # Only update the registry when EVERY pending name resolved.
        # A partial update would silently drop the unresolved names from
        # this primitive's composes tuple — the unresolved entries do get
        # surfaced via ``unresolved`` / the final raise, but mutating the
        # frozen meta with a half-resolved list first leaves the registry
        # in an inconsistent state for any code that inspects it before
        # the raise propagates. Leave the meta alone; the trailing
        # ``raise ValueError`` is the single source of failure.
        if len(extra) != len(pending):
            continue
        new_meta = dataclasses.replace(
            existing,
            composes=tuple(existing.composes) + tuple(extra),
        )
        _REGISTRY[prim_name] = new_meta
        import contextlib as _ctx

        with _ctx.suppress(AttributeError, TypeError):
            new_meta.func._primitive_meta = new_meta  # type: ignore[attr-defined]
    if unresolved:
        # Do NOT clear ``_PENDING_COMPOSES`` on failure. ``register_primitives``
        # leaves ``_REGISTRATION_DONE`` False on raise, so a retry re-enters
        # this function; but module imports are cached, so the decorator
        # side-effects that fill ``_PENDING_COMPOSES`` do not re-fire. Keeping
        # the dict populated means the retry still surfaces the same error
        # rather than silently "succeeding" with a half-resolved registry.
        raise ValueError("composes resolution failed:\n  " + "\n  ".join(sorted(unresolved)))
    _PENDING_COMPOSES.clear()


def register_primitives() -> None:
    """Import every primitive-bearing module and finalize the registry.

    Must be called once before the registry is queried. Idempotent —
    re-calling is a no-op.

    Walks the packages listed in :data:`_PRIMITIVE_PACKAGES`, importing
    every public submodule. Each module-level ``@primitive(...)`` call
    registers itself in ``_REGISTRY`` as an import side-effect. After
    every module has been imported (core + plugins), runs
    :func:`_finalize_composes` to resolve string-name ``composes=``
    against the now-complete registry.

    Module-import errors fail loudly for core packages (a missing
    decorator must surface immediately, not as a downstream "primitive
    not in registry" warning); plugin modules log-and-skip per the
    plugins.load_plugins contract.
    """
    global _REGISTRATION_DONE
    if _REGISTRATION_DONE:
        return
    for modname in _discover_primitive_modules():
        importlib.import_module(modname)
    # Optional plugin distributions contribute extra primitive modules
    # via the ``hpc_agent.plugins`` entry-point group. With none
    # installed this is an empty loop and registration is unchanged.
    from hpc_agent._kernel.registry.plugins import plugin_primitive_modules

    for modname in plugin_primitive_modules():
        # Core modules above fail loudly; an optional plugin must not
        # take down the core CLI (matches plugins.load_plugins' skip-on-
        # error contract). A plugin whose entry point loaded but whose
        # listed module fails to import is logged and skipped.
        try:
            importlib.import_module(modname)
        except Exception as exc:  # noqa: BLE001 — broken plugin must not crash core
            import warnings

            warnings.warn(
                f"hpc-agent plugin module {modname!r} failed to import; "
                f"its primitives are unavailable: {exc}",
                stacklevel=2,
            )
    _finalize_composes()
    _REGISTRATION_DONE = True


def get_registry() -> dict[str, PrimitiveMeta]:
    """Return a snapshot of the primitive registry.

    Raises ``RuntimeError`` if :func:`register_primitives` has not yet
    been called. Tests configure an autouse session fixture; the
    ``hpc-agent`` CLI invokes it from ``main()`` before subcommand
    dispatch.
    """
    if not _REGISTRATION_DONE:
        raise RuntimeError(
            "Primitive registry queried before register_primitives() "
            "was called. Call hpc_agent.register_primitives() at "
            "process startup."
        )
    return dict(_REGISTRY)


def get_meta(name: str) -> PrimitiveMeta:
    """Return the :class:`PrimitiveMeta` for ``name`` (KeyError if absent)."""
    if not _REGISTRATION_DONE:
        raise RuntimeError(
            "Primitive registry queried before register_primitives() "
            "was called. Call hpc_agent.register_primitives() at "
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
