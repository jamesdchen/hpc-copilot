"""Argparse orchestrator — auto-registers CliShape primitives, falls through to legacy.

:func:`build_parser` is the single source of truth for the
``hpc-agent`` CLI surface. It walks the primitive registry and
auto-registers a subcommand for every primitive whose ``cli`` is a
:class:`CliShape`; anything still carrying a legacy ``cli=str`` (or
``cli=None``) falls through to the hand-written
:func:`hpc_agent.agent_cli._register_legacy_subcommands`.

The fallback is the migration safety net: during the multi-PR
migration each per-domain PR moves a handful of primitives from the
legacy body to ``CliShape`` declarations, and the dispatcher picks
them up automatically. After Phase 3 lands, the fallback drops out
and the legacy body shrinks to ``pass``.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import hpc_agent
from hpc_agent.cli._dispatch import CliShape, _leaf_verb, dispatch_primitive

if TYPE_CHECKING:
    pass


# Help text for parent verb-group parsers. The dispatcher computes the
# leaf verb name; the parent's help string is hand-curated here because
# it's a small fixed set and the alternative (looking at the first
# child primitive's group_help) is fragile.
_GROUP_HELP: dict[str, str] = {
    "campaign": "Closed-loop campaign read-only commands (status, list, init, ...).",
    "clusters": "Introspect available cluster definitions.",
    "validate": "Validators (campaign, ...).",
    "build": (
        "Scaffolders (axes-init, build-executor, build-template, "
        "build-tasks-py, build-submit-spec)."
    ),
}


def _add_standard_args(parser: argparse.ArgumentParser, shape: CliShape) -> None:
    """Inject the spec / experiment-dir / dry-run / per-primitive args."""
    from hpc_agent.cli._helpers import _add_experiment_dir

    if shape.spec_arg:
        schema_hint = (
            f"schemas/{shape.schema_ref.input}.input.json"
            if shape.schema_ref and shape.schema_ref.input
            else "JSON spec"
        )
        parser.add_argument(
            "--spec",
            type=__import__("pathlib").Path,
            required=shape.spec_required,
            help=f"JSON spec file ({schema_hint})",
        )
    if shape.experiment_dir_arg:
        _add_experiment_dir(parser)
    if shape.dry_run_arg:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Resolve and emit the spec without executing.",
        )
    for arg in shape.args:
        arg.add_to(parser)


def _bind_dispatch(parser: argparse.ArgumentParser, name: str) -> None:
    """Wire ``parser.set_defaults(func=...)`` to the generic dispatcher."""

    def _func(ns: argparse.Namespace, _name: str = name) -> int:
        return dispatch_primitive(_name, ns)

    parser.set_defaults(func=_func)


def _register_from_registry(
    sub: argparse._SubParsersAction,
) -> dict[str, argparse._SubParsersAction]:
    """Register every primitive whose ``cli`` is a :class:`CliShape`.

    Returns a map of group-name → nested SubParsersAction so the legacy
    body can locate the parent of a verb-group (e.g. ``campaign``) if it
    still owns sibling subcommands during the migration.
    """
    from hpc_agent._internal.primitive import get_registry

    registry = get_registry()
    nested_groups: dict[str, argparse._SubParsersAction] = {}

    for name, meta in registry.items():
        shape = meta.cli
        if not isinstance(shape, CliShape):
            continue

        verb = _leaf_verb(name, shape)

        if shape.group is None:
            parser = sub.add_parser(verb, help=shape.help)
            _add_standard_args(parser, shape)
            _bind_dispatch(parser, name)
            continue

        # Verb-grouped primitive: ensure the parent exists, then nest.
        group_sub = nested_groups.get(shape.group)
        if group_sub is None:
            existing = sub.choices.get(shape.group)
            if existing is not None:
                # Parent created by the legacy body; reuse its
                # _SubParsersAction so leaf verbs nest under it.
                for action in existing._actions:
                    if isinstance(action, argparse._SubParsersAction):
                        group_sub = action
                        break
            if group_sub is None:
                parent = sub.add_parser(
                    shape.group,
                    help=_GROUP_HELP.get(shape.group, f"{shape.group} verb-group commands."),
                )
                group_sub = parent.add_subparsers(dest="action", required=True)
            nested_groups[shape.group] = group_sub

        parser = group_sub.add_parser(verb, help=shape.help)
        _add_standard_args(parser, shape)
        _bind_dispatch(parser, name)

    return nested_groups


def _register_tier3_modules(sub: argparse._SubParsersAction) -> None:
    """Register CLI-only verb modules that have no @primitive backing.

    Each Tier 3 module owns its ``register(sub)`` entry point;
    aggregating them here keeps per-domain migration PRs additive (a
    new module = one line here, plus the module file).
    """
    from hpc_agent.cli.setup import register as _register_setup

    _register_setup(sub)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``hpc-agent`` argparse tree.

    Order matters: auto-registration runs first so the legacy body
    can detect already-registered verbs (and skip its own ``add_parser``
    call to avoid an ``argparse`` duplicate-name error). During the
    migration the legacy body owns this responsibility; after Phase 3
    drops the fallback, every primitive comes from the registry walk.
    """
    parser = argparse.ArgumentParser(
        prog="hpc-agent",
        description=(
            "Submit, track status of, and aggregate parameter-grid HPC experiments. "
            "Stdout is a single-line JSON envelope; stderr is JSON-per-line "
            "log records. See docs/reference/cli-spec.md for full schemas."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {hpc_agent.__version__}",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    nested_groups = _register_from_registry(sub)

    # Tier 3 modules — CLI-only orchestrators with no @primitive backing.
    # Each module owns its add_parser blocks via a ``register(sub)``
    # function. Future Tier 3 agents (spawn, ...) append a call here.
    _register_tier3_modules(sub)

    # Legacy fallback — the hand-written add_parser blocks in agent_cli
    # for primitives that haven't been migrated yet. The fallback is
    # idempotent against the registry walk: it inspects ``sub.choices``
    # and ``nested_groups`` to skip names already taken.
    from hpc_agent.agent_cli import _register_legacy_subcommands

    _register_legacy_subcommands(sub, nested_groups=nested_groups)

    # Plugins register at the very end so they can override / extend
    # core verbs. Same hookup as before the registry-driven split.
    from hpc_agent._internal.plugins import register_plugin_cli

    register_plugin_cli(sub)

    return parser


__all__ = ["build_parser"]
