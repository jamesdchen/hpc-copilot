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

Source-of-truth split
---------------------

The registry IS the canonical source for the structured metadata
the decorator carries: ``name``, ``verb``, ``side_effects``,
``idempotent``, ``idempotency_key``, ``error_codes``, ``composes``,
and ``cli`` (the shell invocation string).
:func:`hpc_agent._internal.operations.operations_catalog` reads the
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
            cli=cli,
            agent_facing=agent_facing,
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
    "hpc_agent.state.runs",
    "hpc_agent.state.discover",
    "hpc_agent.planning.resubmit_batching",
    "hpc_agent.atoms.campaign_health",
    "hpc_agent.infra.clusters",
    "hpc_agent.agent_cli",
    "hpc_agent.atoms.axes_init",
    "hpc_agent.atoms.aggregation_invariants",
    "hpc_agent.atoms.build_executor",
    "hpc_agent.atoms.build_submit_spec",
    "hpc_agent.atoms.build_tasks_py",
    "hpc_agent.atoms.build_template",
    "hpc_agent.atoms.canary_verify",
    "hpc_agent.atoms.cluster_reduce",
    "hpc_agent.atoms.campaign_advance",
    "hpc_agent.atoms.campaign_budget",
    "hpc_agent.atoms.campaign_converged",
    "hpc_agent.atoms.campaign_init",
    "hpc_agent.atoms.campaign_list",
    "hpc_agent.atoms.campaign_replay",
    "hpc_agent.atoms.campaign_status",
    "hpc_agent.atoms.capabilities",
    "hpc_agent.atoms.classify_axis",
    "hpc_agent.atoms.clusters",
    "hpc_agent.atoms.export_package",
    "hpc_agent.atoms.failures",
    "hpc_agent.atoms.interview",
    "hpc_agent.atoms.list_in_flight",
    "hpc_agent.atoms.load_context",
    "hpc_agent.atoms.logs",
    "hpc_agent.atoms.monitor_arm",
    "hpc_agent.atoms.monitor_summary",
    "hpc_agent.atoms.plan_throughput",
    "hpc_agent.ops.preflight.check",
    "hpc_agent.atoms.recall",
    "hpc_agent.atoms.recommend_partition",
    "hpc_agent.atoms.setup_actions",
    "hpc_agent.atoms.submit_plan_summary",
    "hpc_agent.atoms.validate_executor_signatures",
    "hpc_agent.atoms.validate_input_dataset",
    "hpc_agent.atoms.validate_self_qos_limit",
    "hpc_agent.atoms.validate_stochastic_marker",
    "hpc_agent.atoms.validate_walltime_against_history",
    "hpc_agent.runner.submit",
    "hpc_agent.runner.status",
    "hpc_agent.runner.combine",
    "hpc_agent.runner.resubmit",
    "hpc_agent.runner.reconcile",
    "hpc_agent.runner.update_constraints",
    # Composites — must come after every atom they reference.
    "hpc_agent.flows.submit_flow",
    "hpc_agent.flows.monitor_flow",
    "hpc_agent.flows.aggregate_flow",
    "hpc_agent.flows.validate_campaign",
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
    # Optional plugin distributions contribute extra primitive modules
    # via the ``hpc_agent.plugins`` entry-point group. With none
    # installed this is an empty loop and registration is unchanged.
    from hpc_agent._internal.plugins import plugin_primitive_modules

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
