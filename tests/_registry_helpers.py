"""Helpers for tests that compare the live registry against core-only on-disk artifacts.

When any ``hpc_agent.plugins`` plugin is installed in the same
environment as ``hpc-agent``, the plugin loader discovers the plugin's
primitives through that entry-point group. CI installs only the core
package; the on-disk artifacts shipped with core
(``src/hpc_agent/operations.json``, ``docs/primitives/<name>.md``,
``tests/worker_prompts/fixtures/<workflow>.prefix.txt``, etc.) are
baked from a core-only registry.

Tests that compare the live registry against those artifacts must
filter the plugin primitives out so they pass regardless of whether a
plugin happens to be importable in the dev shell. The CI path is
unaffected because plugin primitives are absent there to begin with —
the filter is a no-op when nothing matches.

Filter rule: a primitive is core-only iff its function's module path
starts with ``hpc_agent.`` (with the dot). Plugin primitives live
under their own ``hpc_agent_<plugin>.<...>`` top-level package, which
starts with ``hpc_agent_`` (no dot) and is excluded.
"""

from __future__ import annotations

from hpc_agent._kernel.registry.operations import operations_catalog as _full_catalog
from hpc_agent._kernel.registry.primitive import (
    PrimitiveMeta,
    get_registry,
    register_primitives,
)


def is_any_plugin_installed() -> bool:
    """True iff at least one ``hpc_agent.plugins`` plugin is loaded.

    Uses the host's own generic discovery seam rather than importing a
    specific plugin package by name — the helper stays agnostic to which
    plugin (if any) is present.
    """
    from hpc_agent._kernel.registry.plugins import load_plugins

    return bool(load_plugins())


def _is_core_primitive(meta: PrimitiveMeta) -> bool:
    """True iff the primitive's underlying function lives in the core package."""
    mod = getattr(meta.func, "__module__", "") or ""
    return mod.startswith("hpc_agent.")


def core_only_registry() -> dict[str, PrimitiveMeta]:
    """Return the @primitive registry, filtered to core-only entries.

    A no-op when no plugin is installed; in that case the result is the
    same as :func:`get_registry`.
    """
    register_primitives()
    return {name: meta for name, meta in get_registry().items() if _is_core_primitive(meta)}


def core_only_operations_catalog() -> list[dict]:
    """Return the operations catalog filtered to core-only primitives.

    Mirrors :func:`hpc_agent._kernel.registry.operations.operations_catalog`
    output exactly (same field shape, same ``(verb, name)`` sort order)
    but drops any entry whose primitive came from a plugin.
    """
    register_primitives()
    core_names = set(core_only_registry().keys())
    return [entry for entry in _full_catalog() if entry["name"] in core_names]


def plugin_overlaid_workflows() -> frozenset[str]:
    """Return the worker-prompt workflows overlaid by any loaded plugin.

    Item 5 moved the overlay surface onto the plugin manifest; tests
    that need to know "which workflow fixtures are stale when a plugin
    is installed" read ``MANIFEST.worker_prompt_overlays`` across every
    loaded plugin instead of maintaining a parallel allowlist. Returns
    an empty frozenset when no plugin is installed.
    """
    from hpc_agent._kernel.registry.plugins import get_plugin_manifests

    overlays: set[str] = set()
    for manifest in get_plugin_manifests().values():
        overlays.update(manifest.worker_prompt_overlays)
    return frozenset(overlays)
