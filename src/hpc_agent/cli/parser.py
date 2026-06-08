"""Argparse orchestrator — auto-registers every primitive from the registry.

:func:`build_parser` is the single source of truth for the
``hpc-agent`` CLI surface. It walks the primitive registry and
auto-registers a subcommand for every primitive whose ``cli`` is a
:class:`CliShape`; Tier 3 verbs (no ``@primitive`` backing) are
registered via :func:`_register_tier3_modules`.
"""

from __future__ import annotations

import argparse
import difflib
import re
from typing import NoReturn

import hpc_agent
from hpc_agent.cli._dispatch import CliShape, _leaf_verb, dispatch_primitive

_INVALID_CHOICE_RE = re.compile(r"invalid choice: '([^']*)'")


class _HpcArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that answers an unknown verb with a compact
    "did you mean" line instead of argparse's full subcommand dump.

    Stock argparse prints the usage line — which for a subparsers action
    lists every one of the ~70 verbs — AND ``invalid choice: 'X' (choose
    from <all of them again>)``: the whole CLI surface, twice. Read back
    into a spawned worker's context that is a heavy, content-free tax
    that also buries the one useful thing — the verb the caller meant.
    We intercept that single error class; all other argparse errors fall
    through to the stock handler.
    """

    def error(self, message: str) -> NoReturn:
        match = _INVALID_CHOICE_RE.search(message)
        if match is not None:
            bad = match.group(1)
            choices = self._subcommand_choices()
            close = difflib.get_close_matches(bad, choices, n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            self.exit(
                2,
                f"{self.prog}: error: unknown command {bad!r}.{hint} "
                f"Run `{self.prog} --help` for the {len(choices)} available commands.\n",
            )
        super().error(message)

    def _subcommand_choices(self) -> list[str]:
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                return list(action.choices)
        return []


# Help text for parent verb-group parsers. The dispatcher computes the
# leaf verb name; the parent's help string is hand-curated here because
# it's a small fixed set.
_GROUP_HELP: dict[str, str] = {
    "campaign": "Closed-loop campaign read-only commands (status, list, init, ...).",
    "clusters": "Introspect available cluster definitions.",
    "recoveries": "Browse the typed recovery registry (list known kinds, show a menu).",
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
    from hpc_agent._kernel.registry.primitive import get_registry

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
    aggregating them here keeps the surface in one place. ``run`` is
    the only remaining Tier 3 verb post-Item-2 — capabilities, setup,
    describe, and install-commands are now ``@primitive`` entries
    picked up by :func:`_register_from_registry`.
    """
    from hpc_agent.cli.spawn import register as _register_spawn

    _register_spawn(sub)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``hpc-agent`` argparse tree.

    Every verb comes from either the registry walk (a primitive's
    :class:`CliShape`) or a Tier 3 module's ``register(sub)``.
    Plugins register last so they can override / extend core verbs.
    """
    parser = _HpcArgumentParser(
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

    from hpc_agent._kernel.registry.plugins import register_plugin_cli

    register_plugin_cli(sub)

    return parser


__all__ = ["build_parser"]
