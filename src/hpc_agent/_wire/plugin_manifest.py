"""Plugin manifest — explicit declaration of what a plugin contributes.

The host package walks the ``hpc_agent.plugins`` entry-point group and
discovers extensions by attribute lookup (``primitive_modules``,
``slash_command_assets``, ``register_cli``, ``worker_prompt_assets``).
Pre-Item-5, the *overlay* surface (whether a plugin overrides a host
worker-prompt procedure, whether it registers CLI subcommands, what
primitive names it claims) was an implicit consequence of the
attribute-existence checks — readers of the plugin object couldn't
tell from a glance what the plugin actually changes about the host.

``PluginManifest`` makes that surface explicit. A plugin declares a
top-level ``MANIFEST = PluginManifest(...)`` listing its name,
version, the primitives it registers, the worker-prompt files it
overlays, and whether it wires CLI subcommands. The host capabilities
envelope projects every loaded plugin's manifest under the new
``plugins`` field; the ``scripts/lint_plugin_manifests.py`` lint
verifies the declarations match what the plugin actually contributes
at runtime.

This is informational metadata, not a hard requirement. Plugins
without a manifest still load (with a ``DeprecationWarning``); the
manifest is what a CLI introspection caller, a CI gate, or a future
``hpc-agent describe <plugin>`` can read without importing the plugin
package itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PluginManifest(BaseModel):
    """Self-declared metadata for one ``hpc_agent.plugins`` entry."""

    model_config = ConfigDict(extra="forbid", title="hpc-agent plugin manifest")

    name: str = Field(description="Plugin distribution name (e.g. ``hpc-agent-pro``).")
    version: str = Field(description="Plugin distribution version (e.g. ``0.6.0``).")
    primitives: tuple[str, ...] = Field(
        default=(),
        description=(
            "Wire names of every primitive this plugin registers. Each name "
            "must appear in the operations catalog after registration; the "
            "``scripts/lint_plugin_manifests.py`` gate verifies the match."
        ),
    )
    worker_prompt_overlays: tuple[str, ...] = Field(
        default=(),
        description=(
            "Workflow names whose ``worker_prompts/<workflow>.md`` this plugin "
            "overlays. The host worker-prompt loader prefers the first plugin "
            "providing a workflow; declaring it here lets the catalog tell a "
            "caller which procedure body they'll actually receive."
        ),
    )
    cli_register: bool = Field(
        default=False,
        description=(
            "Whether this plugin wires CLI subcommands via ``register_cli`` "
            "into the host argparse tree. Informational; the host always "
            "invokes ``register_cli`` if it exists, so the field is honest "
            "with respect to the loader, not an opt-out switch."
        ),
    )
