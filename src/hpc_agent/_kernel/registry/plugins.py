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
* ``schema_assets`` — a traversable directory of wire-schema JSON
  (``<name>.input.json`` / ``<name>.output.json``) for the plugin's
  primitives, consulted by the CLI input boundary so ``--spec``
  validation fires for plugin-owned primitives too. When absent, the
  host falls back to the ``<plugin-module-root>.schemas`` package by
  convention — so a plugin laid out the conventional way needs no
  explicit hook.

All are optional; a plugin may provide any combination, or none.

The core import surface a plugin may reach is frozen + pinned in
``docs/reference/plugin-api-contract.md`` (enforced by
``scripts/lint_plugin_api_surface.py``).
"""

from __future__ import annotations

import os
import warnings
from functools import cache
from importlib.metadata import entry_points
from importlib.resources import files as _resource_files
from typing import Any

__all__ = [
    "PLUGIN_GROUP",
    "cli_reshaping_verdict",
    "get_plugin_manifests",
    "load_plugins",
    "plugin_contributes_primitive_modules",
    "plugin_primitive_modules",
    "plugin_schema_roots",
    "plugin_slash_command_roots",
    "plugin_worker_prompt_roots",
    "register_plugin_cli",
    "run_plugin_setup_actions",
]

PLUGIN_GROUP = "hpc_agent.plugins"
DISABLE_ENV_VAR = "HPC_AGENT_DISABLE_PLUGINS"


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

    ``HPC_AGENT_DISABLE_PLUGINS=1`` short-circuits the entry-point scan
    and returns ``()`` — the chokepoint that makes the dev-loop regen
    scripts (``build_primitive_frontmatter`` / ``build_primitive_index``
    / ``build_operations_index``) produce core-only output even with
    a plugin installed in the venv. Inherits across the
    subprocess used by ``build_operations_index``, so a single env-var
    read at this chokepoint covers every plugin hook below (#198).
    """
    if os.environ.get(DISABLE_ENV_VAR) == "1":
        return ()
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


def plugin_contributes_primitive_modules() -> bool:
    """True when at least one installed plugin contributes ``primitive_modules``.

    The baked ``operations.json`` catalog is CORE-ONLY (it is projected with the
    plugin scan disabled — see ``scripts/bake_operations_json.py``). So the CLI
    discovery verbs (``describe`` / ``find``) may only answer off the bake — the
    baked-hydration fast path — when NO plugin adds primitive modules; otherwise
    the bake would miss the plugin's verbs and the fast path would disagree with
    the full walk (which imports every ``primitive_modules`` module). This is the
    ``primitive_modules`` half of the gate ``load_baked_catalog``'s docstring
    already asserts ("gated off when any plugin contributes primitive modules or
    reshapes a verb"); the ``register_cli`` reshaping half is
    :func:`cli_reshaping_verdict`.

    A plugin that reshapes the CLI but adds no primitives, or adds neither,
    contributes ``False`` here — that plugin is handled by the reshaping gate.
    """
    return bool(plugin_primitive_modules())


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


def cli_reshaping_verdict() -> tuple[bool, frozenset[str]]:
    """Return the CLI single-verb fast-path verdict from the installed plugins.

    The fast path (``hpc_agent.cli.dispatch``) imports only the one module a
    mapped core verb needs, skipping the full registry walk — the sole place a
    plugin ``register_cli`` hook reshapes a core verb's parser. So the fast path
    must defer to the walk for any verb a plugin can reshape. This function
    reduces the loaded plugin set to the two facts the per-verb gate needs:

    * ``conservative_full_walk`` — ``True`` when at least one plugin exposes a
      callable ``register_cli`` but does NOT declare (via its manifest's
      ``reshapes_core_verbs``) which core verbs it reshapes. An UNDECLARED
      reshaper could touch anything, so — exactly as before this field existed —
      EVERY core verb falls back to the full walk. Legacy plugins (no manifest,
      or a manifest predating the field) get today's conservative safety.
    * ``reshaped_verbs`` — the union of every DECLARED reshaper's
      ``reshapes_core_verbs``. When ``conservative_full_walk`` is ``False``,
      only these named verbs take the full walk; every other core verb stays
      fast — the add-only-plugin case (``reshapes_core_verbs=()``) contributes
      nothing here, so core verbs keep the fast path.

    A plugin with no callable ``register_cli`` cannot reshape any parser and is
    ignored regardless of its manifest — its new primitives are absent from
    ``VERB_MODULE_MAP`` and fall through on their own.
    """
    from hpc_agent._wire.plugin_manifest import PluginManifest

    conservative = False
    reshaped: set[str] = set()
    for plugin in load_plugins():
        if not callable(getattr(plugin, "register_cli", None)):
            continue
        manifest = getattr(plugin, "MANIFEST", None)
        declared = manifest.reshapes_core_verbs if isinstance(manifest, PluginManifest) else None
        if declared is None:
            # Undeclared reshaper: cannot narrow → keep the conservative gate.
            conservative = True
            continue
        reshaped.update(declared)
    return conservative, frozenset(reshaped)


def run_plugin_setup_actions(context: dict[str, Any]) -> dict[str, Any]:
    """Collect every plugin's optional ``setup`` contributions.

    A plugin may expose a ``run_setup_actions(context) -> Mapping | None``
    callable. The host's ``setup`` primitive invokes it — passing a
    context dict (``cluster``, ``experiment_dir``, ``install``,
    ``dry_run``) — and merges each plugin's returned mapping into the
    ``setup`` envelope's ``plugin_actions`` field, keyed by the plugin's
    manifest name (falling back to the plugin module name).

    This is the generic seam that replaces host-side knowledge of any
    specific plugin verb: the host invokes the hook blindly and never
    names what a plugin does at setup time. A plugin without the hook,
    or one whose hook returns ``None``/empty, contributes nothing. A
    hook that raises is isolated so one plugin can't break ``setup`` —
    the failure surfaces as a ``warnings.warn`` and that plugin's entry
    is omitted.
    """
    actions: dict[str, Any] = {}
    for plugin in load_plugins():
        hook = getattr(plugin, "run_setup_actions", None)
        if not callable(hook):
            continue
        try:
            result = hook(dict(context))
        except Exception as exc:  # noqa: BLE001 — a plugin hook may raise anything
            warnings.warn(
                f"plugin setup hook failed for {getattr(plugin, '__name__', plugin)!r}: {exc!r}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        if not result:
            continue
        key = _plugin_key(plugin)
        actions[key] = dict(result)
    return actions


def _plugin_key(plugin: Any) -> str:
    """Stable key for a plugin in the ``plugin_actions`` map.

    Prefers the manifest name (the operator-facing distribution name);
    falls back to the plugin object's ``__name__`` top-level package.
    """
    manifest = getattr(plugin, "MANIFEST", None)
    name = getattr(manifest, "name", None)
    if isinstance(name, str) and name:
        return name
    mod = getattr(plugin, "__name__", None)
    if isinstance(mod, str) and mod:
        return mod.split(".", 1)[0]
    return repr(plugin)


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


def _conventional_schema_package(plugin: Any) -> str | None:
    """Derive the ``<plugin-root>.schemas`` package name from *plugin*.

    The plugin entry point points at a module
    (``hpc_agent_myplugin.plugin``) or a package; its top-level
    distribution package is the first dotted segment of ``__name__``. By
    convention a plugin keeps its wire
    schemas in ``<that package>.schemas`` — the same ``schemas/`` layout
    the host uses. Returns ``None`` for a plugin object without a usable
    ``__name__`` (e.g. an instance), in which case it must expose an
    explicit ``schema_assets`` to participate.
    """
    name = getattr(plugin, "__name__", None)
    if not isinstance(name, str) or not name:
        return None
    return f"{name.split('.', 1)[0]}.schemas"


def plugin_schema_roots() -> tuple[Any, ...]:
    """Return the wire-schema asset roots contributed by plugins.

    Each element is an :mod:`importlib.resources` traversable directory
    holding ``<name>.input.json`` / ``<name>.output.json`` files for a
    plugin's primitives. A plugin may name the directory explicitly via a
    ``schema_assets`` attribute; otherwise the host resolves the
    conventional ``<plugin-module-root>.schemas`` package (see
    :func:`_conventional_schema_package`). A plugin that has neither
    contributes no root and is simply skipped.

    Consumed by the CLI input boundary
    (``hpc_agent.cli._helpers._validate_against_schema``) so ``--spec``
    validation resolves a plugin-owned primitive's schema after the
    core ``hpc_agent.schemas`` lookup — previously this iterated a
    hard-coded plugin package name, so only one specific plugin's
    schemas were ever found.
    """
    roots: list[Any] = []
    for plugin in load_plugins():
        root = getattr(plugin, "schema_assets", None)
        if root is None:
            pkg = _conventional_schema_package(plugin)
            if pkg is None:
                continue
            try:
                root = _resource_files(pkg)
            except (ModuleNotFoundError, ImportError):
                continue
        roots.append(root)
    return tuple(roots)
