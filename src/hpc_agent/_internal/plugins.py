"""Discovery of optional hpc-agent plugins.

A plugin is a separate installed distribution that extends hpc-agent
with additional primitives and CLI subcommands. Plugins are discovered
through the ``hpc_agent.plugins`` entry-point group: installing a plugin
distribution is the entire opt-in, and with none installed every
function here returns an empty result so the core package is wholly
unaffected.

A plugin's entry point resolves to any object exposing the optional
attributes of the informal plugin contract:

* ``primitive_modules: Iterable[str]`` — dotted module paths imported
  (after the core modules) so their ``@primitive`` decorators register.
* ``register_cli(subparsers) -> None`` — callable handed the CLI's
  argparse subparsers action so the plugin can add subcommands.
* ``slash_command_assets`` — a traversable directory holding
  ``commands/`` and/or ``skills/`` subtrees, installed over the core
  assets by ``hpc-agent install-commands``.

All are optional; a plugin may provide any combination, or none.
"""

from __future__ import annotations

from functools import cache
from importlib.metadata import entry_points
from typing import Any

__all__ = [
    "PLUGIN_GROUP",
    "load_plugins",
    "plugin_primitive_modules",
    "plugin_slash_command_roots",
    "register_plugin_cli",
]

PLUGIN_GROUP = "hpc_agent.plugins"


@cache
def load_plugins() -> tuple[Any, ...]:
    """Return the loaded objects for every registered plugin entry point.

    Cached: entry-point resolution touches installed-distribution
    metadata and the set cannot change within a process. A plugin whose
    entry point fails to import is skipped rather than crashing the
    host — a broken optional plugin must not take down the core CLI.
    """
    loaded: list[Any] = []
    for ep in entry_points(group=PLUGIN_GROUP):
        try:
            loaded.append(ep.load())
        except Exception:
            continue
    return tuple(loaded)


def plugin_primitive_modules() -> tuple[str, ...]:
    """Return every plugin-contributed primitive module path, in plugin order."""
    modules: list[str] = []
    for plugin in load_plugins():
        modules.extend(getattr(plugin, "primitive_modules", ()) or ())
    return tuple(modules)


def register_plugin_cli(subparsers: Any) -> None:
    """Let every plugin add its subcommands to the CLI's *subparsers*."""
    for plugin in load_plugins():
        hook = getattr(plugin, "register_cli", None)
        if callable(hook):
            hook(subparsers)


def plugin_slash_command_roots() -> tuple[Any, ...]:
    """Return the slash-command asset roots contributed by plugins.

    Each element is an :mod:`importlib.resources` traversable directory
    holding ``commands/`` and/or ``skills/`` subtrees, exposed by a
    plugin through its ``slash_command_assets`` attribute. Order follows
    plugin-discovery order; ``install-commands`` applies them after the
    core assets so a later writer wins.
    """
    roots: list[Any] = []
    for plugin in load_plugins():
        root = getattr(plugin, "slash_command_assets", None)
        if root is not None:
            roots.append(root)
    return tuple(roots)
