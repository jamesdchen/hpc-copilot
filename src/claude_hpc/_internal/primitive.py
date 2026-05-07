"""Primitive registry — runtime catalog of all hpc-agent primitives.

Implementation is SoT for behavior (the decorated function). JSON
schemas under ``claude_hpc/schemas/`` are SoT for the wire contract.
``docs/primitives/*.md`` and ``operations.py``'s catalog are *views*
generated from this registry plus the schemas.

Decoration convention
---------------------

Most primitives have a clean Python entry point — a public function in one
of the engine subpackages (``runner/``, ``planning/``, ``state/``,
``forecast/``, ``flows/``, or ``infra/``) that performs the operation.
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
:func:`claude_hpc._internal.operations.operations_catalog` reads the
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

    from claude_hpc.errors import HpcError


VerbKind = Literal["query", "validate", "mutate", "submit", "scaffold", "workflow"]

# Preserve the decorated function's signature so mypy sees the original
# return type at call sites (the decorator returns the func unchanged).
F = TypeVar("F", bound="Callable[..., Any]")


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
    # Shell invocation string (e.g. ``"hpc-agent build-executor --name <stem>"``)
    # or ``None`` for Python-only primitives. Previously round-tripped
    # through ``docs/primitives/<name>.md`` frontmatter; the registry
    # is now SoT so the regen script writes ``backed_by.cli`` from here.
    cli: str | None = None
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
    cli: str | None = None,
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
    "claude_hpc.state.runs",
    "claude_hpc.state.runtime_prior",
    "claude_hpc.forecast.calibration",
    "claude_hpc.forecast.best_submit_window",
    "claude_hpc.forecast.queue_wait_baseline",
    "claude_hpc.state.discover",
    "claude_hpc.planning.resubmit_batching",
    "claude_hpc.planning.planner",
    "claude_hpc.atoms.campaign_health",
    "claude_hpc.infra.inspect",
    "claude_hpc.infra.clusters",
    "claude_hpc.agent_cli",
    "claude_hpc.atoms.axes_init",
    "claude_hpc.atoms.aggregation_invariants",
    "claude_hpc.atoms.build_executor",
    "claude_hpc.atoms.build_submit_spec",
    "claude_hpc.atoms.build_tasks_py",
    "claude_hpc.atoms.canary_verify",
    "claude_hpc.atoms.cluster_reduce",
    "claude_hpc.atoms.campaign_advance",
    "claude_hpc.atoms.campaign_budget",
    "claude_hpc.atoms.campaign_converged",
    "claude_hpc.atoms.campaign_init",
    "claude_hpc.atoms.campaign_list",
    "claude_hpc.atoms.campaign_replay",
    "claude_hpc.atoms.campaign_status",
    "claude_hpc.atoms.capabilities",
    "claude_hpc.atoms.clusters",
    "claude_hpc.atoms.failures",
    "claude_hpc.atoms.house_edge",
    "claude_hpc.atoms.interview",
    "claude_hpc.atoms.list_in_flight",
    "claude_hpc.atoms.logs",
    "claude_hpc.atoms.monitor_arm",
    "claude_hpc.atoms.monitor_summary",
    "claude_hpc.atoms.predict_start_time",
    "claude_hpc.atoms.preflight",
    "claude_hpc.atoms.recall",
    "claude_hpc.atoms.recommend_partition",
    "claude_hpc.atoms.recommend_wait_alternative",
    "claude_hpc.atoms.setup_actions",
    "claude_hpc.atoms.submit_plan_summary",
    "claude_hpc.atoms.validate_executor_signatures",
    "claude_hpc.atoms.validate_input_dataset",
    "claude_hpc.atoms.validate_self_qos_limit",
    "claude_hpc.atoms.validate_walltime_against_history",
    "claude_hpc.atoms.walltime_drift",
    "claude_hpc.runner.submit",
    "claude_hpc.runner.status",
    "claude_hpc.runner.combine",
    "claude_hpc.runner.resubmit",
    "claude_hpc.runner.reconcile",
    "claude_hpc.runner.update_constraints",
    "claude_hpc.planning.validate",
    # Composites — must come after every atom they reference.
    "claude_hpc.flows.submit_flow",
    "claude_hpc.flows.monitor_flow",
    "claude_hpc.flows.aggregate_flow",
    "claude_hpc.flows.validate_campaign",
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
    ``hpc-agent`` CLI invokes it from ``main()`` before subcommand
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
