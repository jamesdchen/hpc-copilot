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

import warnings
from functools import cache
from importlib.metadata import entry_points
from typing import Any

__all__ = [
    "PLUGIN_GROUP",
    "get_plugin_manifests",
    "load_plugins",
    "plugin_primitive_modules",
    "plugin_slash_command_roots",
    "plugin_worker_prompt_roots",
    "register_plugin_cli",
]

PLUGIN_GROUP = "hpc_agent.plugins"


@cache
def load_plugins() -> tuple[Any, ...]:
    """Return the loaded objects for every registered plugin entry point.

    Cached: entry-point resolution touches installed-distribution
    metadata and the set cannot change within a process. A plugin whose
    entry point fails to import is skipped rather than crashing the
    host — a broken optional plugin must not take down the core CLI —
    but the failure is surfaced via :func:`warnings.warn` so the
    operator notices a silently-disabled plugin (was previously a bare
    ``continue`` that swallowed every load error).
    """
    loaded: list[Any] = []
    for ep in entry_points(group=PLUGIN_GROUP):
        try:
            loaded.append(ep.load())
        except Exception as exc:  # noqa: BLE001 — entry-point load may raise anything
            warnings.warn(
                f"failed to load hpc-agent plugin {ep.name!r} (entry_point={ep.value!r}): {exc!r}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
    return tuple(loaded)


def plugin_primitive_modules() -> tuple[str, ...]:
    """Return every plugin-contributed primitive module path, in plugin order."""
    modules: list[str] = []
    for plugin in load_plugins():
        modules.extend(getattr(plugin, "primitive_modules", ()) or ())
    return tuple(modules)


@cache
def get_plugin_manifests() -> dict[str, Any]:
    """Return the :class:`PluginManifest` for every loaded plugin, keyed by name.

    A plugin exposes its manifest at module scope as ``MANIFEST``. The
    field is informational — plugins without a manifest still load but
    emit a :class:`DeprecationWarning` so the operator notices the
    missing self-description, and the catalog projects them with a
    synthesised stub. Item 5 added the manifest as the explicit
    declaration surface; the implicit attribute lookup that pre-Item-5
    drove plugin behaviour is unchanged underneath.
    """
    from hpc_agent._wire.plugin_manifest import PluginManifest

    manifests: dict[str, Any] = {}
    for plugin in load_plugins():
        manifest = getattr(plugin, "MANIFEST", None)
        if not isinstance(manifest, PluginManifest):
            name = getattr(plugin, "__name__", repr(plugin))
            warnings.warn(
                f"hpc-agent plugin {name!r} does not declare a "
                "``MANIFEST = PluginManifest(...)`` top-level — Item 5 "
                "introduced the manifest as the explicit declaration "
                "surface. The plugin still loads, but its overlay "
                "contributions cannot be enumerated in the capabilities "
                "envelope's ``plugins`` field.",
                DeprecationWarning,
                stacklevel=2,
            )
            continue
        manifests[manifest.name] = manifest
    return manifests


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


def plugin_worker_prompt_roots() -> tuple[Any, ...]:
    """Return the worker-prompt asset roots contributed by plugins.

    Each element is an :mod:`importlib.resources` traversable directory
    holding ``<workflow>.md`` files (``submit.md``, ``status.md``,
    ``aggregate.md``, ``campaign.md``), exposed by a plugin through its
    ``worker_prompt_assets`` attribute. Resolved by
    :func:`hpc_agent._kernel.extension.spawn_prompt._procedure_body`: the first
    plugin to provide ``<workflow>.md`` wins, then the host's bundled
    procedure is used. Distinct from
    :func:`plugin_slash_command_roots` because worker prompts and Claude
    Code skills are different surfaces with different consumers — see
    ``docs/internals/skill-policy.md``.
    """
    roots: list[Any] = []
    for plugin in load_plugins():
        root = getattr(plugin, "worker_prompt_assets", None)
        if root is not None:
            roots.append(root)
    return tuple(roots)
