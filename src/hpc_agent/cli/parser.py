"""Argparse orchestrator — auto-registers every primitive from the registry.

:func:`build_parser` is the single source of truth for the
``hpc-agent`` CLI surface. It walks the primitive registry and
auto-registers a subcommand for every primitive whose ``cli`` is a
:class:`CliShape`; Tier 3 verbs (no ``@primitive`` backing) are
registered via :func:`_register_tier3_modules`.
"""

from __future__ import annotations

import argparse

import hpc_agent
from hpc_agent.cli._dispatch import CliShape, _leaf_verb, dispatch_primitive

# Help text for parent verb-group parsers. The dispatcher computes the
# leaf verb name; the parent's help string is hand-curated here because
# it's a small fixed set.
_GROUP_HELP: dict[str, str] = {
    "campaign": "Closed-loop campaign read-only commands (status, list, init, ...).",
    "clusters": "Introspect available cluster definitions.",
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


def _register_from_registry(sub: argparse._SubParsersAction) -> None:
    """Register every primitive whose ``cli`` is a :class:`CliShape`."""
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
            parent = sub.add_parser(
                shape.group,
                help=_GROUP_HELP.get(shape.group, f"{shape.group} verb-group commands."),
            )
            group_sub = parent.add_subparsers(dest="action", required=True)
            nested_groups[shape.group] = group_sub

        parser = group_sub.add_parser(verb, help=shape.help)
        _add_standard_args(parser, shape)
        _bind_dispatch(parser, name)


def _register_tier3_modules(sub: argparse._SubParsersAction) -> None:
    """Register CLI-only verb modules that have no @primitive backing.

    Each Tier 3 module owns its ``register(sub)`` entry point;
    aggregating them here keeps the surface in one place.
    """
    from hpc_agent.cli.setup import register as _register_setup
    from hpc_agent.cli.spawn import register as _register_spawn

    _register_setup(sub)
    _register_spawn(sub)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``hpc-agent`` argparse tree.

    Every verb comes from either the registry walk (a primitive's
    :class:`CliShape`) or a Tier 3 module's ``register(sub)``.
    Plugins register last so they can override / extend core verbs.
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

    _register_from_registry(sub)
    _register_tier3_modules(sub)

    from hpc_agent._internal.plugins import register_plugin_cli

    register_plugin_cli(sub)

    return parser


__all__ = ["build_parser"]
