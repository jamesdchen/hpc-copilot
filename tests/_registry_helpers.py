"""Helpers for tests that compare the live registry against core-only on-disk artifacts.

When ``hpc-agent-pro`` is installed in the same environment as
``hpc-agent``, the plugin loader discovers the plugin's primitives
through the ``hpc_agent.plugins`` entry-point group. CI installs only
the core package; the on-disk artifacts shipped with core
(``src/hpc_agent/operations.json``, ``docs/primitives/<name>.md``,
``tests/worker_prompts/fixtures/<workflow>.prefix.txt``, etc.) are
baked from a core-only registry.

Tests that compare the live registry against those artifacts must
filter the plugin primitives out so they pass regardless of whether
``hpc-agent-pro`` happens to be importable in the dev shell. The CI
path is unaffected because plugin primitives are absent there to
begin with — the filter is a no-op when nothing matches.

Filter rule: a primitive is core-only iff its function's module path
starts with ``hpc_agent.`` (with the dot). Plugin primitives live
under ``hpc_agent_pro.<...>`` which starts with ``hpc_agent_`` (no
dot) and is excluded.
"""

from __future__ import annotations

from hpc_agent._kernel.registry.operations import operations_catalog as _full_catalog
from hpc_agent._kernel.registry.primitive import (
    PrimitiveMeta,
    get_registry,
    register_primitives,
)


def is_pro_installed() -> bool:
    """True iff ``hpc_agent_pro`` is importable in this environment."""
    try:
        import hpc_agent_pro  # noqa: F401
    except ImportError:
        return False
    return True


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


def pro_overlaid_workflows() -> frozenset[str]:
    """Return the set of worker-prompt workflows hpc-agent-pro overlays.

    Item 5 moved the overlay surface onto the plugin manifest; tests
    that need to know "which workflow fixtures are stale when pro is
    installed" read ``MANIFEST.worker_prompt_overlays`` instead of
    maintaining a parallel allowlist. Returns an empty frozenset when
    the plugin isn't installed.
    """
    if not is_pro_installed():
        return frozenset()
    try:
        from hpc_agent_pro.plugin import MANIFEST  # noqa: PLC0415
    except ImportError:
        return frozenset()
    return frozenset(MANIFEST.worker_prompt_overlays)
