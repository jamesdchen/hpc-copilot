"""hpc_agent._kernel.extension — kernel-to-agent extension surface.

Members:

* :mod:`hpc_agent._kernel.extension.capabilities` — operations catalog
  envelope (kernel introspection).
* :mod:`hpc_agent._kernel.extension.spawn_prompt` — spawn-contract
  render/parse for workflow subagents.
* :mod:`hpc_agent._kernel.extension.telemetry` — telemetry surface.
* :mod:`hpc_agent._kernel.extension.version` — version manifest.
* :mod:`hpc_agent._kernel.extension.worker_prompts` — worker procedure
  markdown package, loaded via ``importlib.resources``.

Eager re-exports are avoided; each member is imported by its callers
directly.
"""
