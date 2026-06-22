"""hpc-agent-github-actions — run task-array fan-outs on GitHub Actions runners.

Registration path, identical to the built-in SGE/SLURM backends:

1. The ``hpc_agent.plugins`` entry point (``pyproject.toml``) resolves here.
2. The host imports every module in ``primitive_modules`` for its side effects;
   importing ``hpc_agent_github_actions.backend`` runs
   ``@register("github-actions")`` on the backend class, so
   ``get_backend("github-actions")`` resolves with zero host edits.
3. ``MANIFEST`` declares the contributed surface for ``hpc-agent capabilities``.

With the plugin installed, a ``clusters.yaml`` entry may set
``scheduler: github-actions`` directly (the host's config validator accepts any
plugin-registered backend name), and submit-flow constructs the backend through
``GitHubActionsBackend.from_build_context`` — the host's construction seam for
non-SSH backends — reading ``$HPC_GHA_*`` / ``$GITHUB_TOKEN`` instead of the SSH
fields.

Unlike a cluster scheduler, GitHub Actions has no login node and no shared
filesystem: code reaches the runner via ``actions/checkout`` and results come
back as artifacts. ``README.md`` spells out which slices of the submit/monitor
flow this backend covers end-to-end and which still need the host's
shared-filesystem reads bridged.
"""

from __future__ import annotations

from hpc_agent._wire.plugin_manifest import PluginManifest

__version__ = "0.1.0"

# Imported by the host for registration side effects. backend.py registers an
# HPCBackend rather than @primitive operations; the import-time seam is the same.
primitive_modules = ("hpc_agent_github_actions.backend",)

MANIFEST = PluginManifest(
    name="hpc-agent-github-actions",
    version=__version__,
    primitives=(),  # contributes only a backend
    worker_prompt_overlays=(),
    cli_register=False,
)
