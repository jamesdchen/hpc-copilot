"""hpc-agent-vastai — skeleton crowd-compute backend plugin.

This package demonstrates the full registration path for a
crowd-compute backend without implementing any of it:

1. The ``hpc_agent.plugins`` entry point (see ``pyproject.toml``)
   resolves to this module.
2. The host imports every module named in ``primitive_modules`` for
   its registration side effects. ``hpc_agent_vastai.backend`` uses
   that import to run ``@register("vastai")`` on the backend class —
   the same decorator the built-in SGE/SLURM backends use — so
   ``get_backend("vastai")`` resolves with zero host edits.
3. ``MANIFEST`` declares the contributed surface explicitly so
   ``hpc-agent capabilities`` can project it without importing the
   platform SDK.

With this plugin installed, a clusters.yaml entry may name
``scheduler: vastai`` directly (the host's config validator accepts
any plugin-registered backend name; no ``scheduler_profile`` pin
needed), and the submit flow constructs the backend through
``VastAIBackend.from_build_context`` — the host's construction seam
for non-SSH backends.

What this skeleton does NOT do: make API calls. Every compute method
raises ``NotImplementedError`` until the real implementation lands
(see ``docs/proposals/crowd-compute-backend.md`` in the hpc-agent
repo).
"""

from __future__ import annotations

from hpc_agent._wire.plugin_manifest import PluginManifest

__version__ = "0.0.1"

# Imported by the host for registration side effects. backend.py
# registers an HPCBackend rather than @primitive operations; the hook
# is the same import-time seam either way.
primitive_modules = ("hpc_agent_vastai.backend",)

MANIFEST = PluginManifest(
    name="hpc-agent-vastai",
    version=__version__,
    primitives=(),  # none yet — the skeleton contributes only a backend
    worker_prompt_overlays=(),
    cli_register=False,
)
