# hpc-agent-vastai (skeleton)

A **non-functional** skeleton showing how a crowd-compute backend
plugs into hpc-agent as a plugin distribution. Every compute method
raises `NotImplementedError` with the mapping the real implementation
must satisfy. Seam analysis and the two deferred host-side edits:
[`docs/proposals/crowd-compute-backend.md`](../../../docs/proposals/crowd-compute-backend.md).

## What it demonstrates

- **Discovery**: the `hpc_agent.plugins` entry point in
  `pyproject.toml` is the entire opt-in — installing the package makes
  the host find it.
- **Registration**: `primitive_modules` names `hpc_agent_vastai.backend`,
  whose import runs `@register("vastai")`; after that,
  `get_backend("vastai")` resolves like `"sge"` or `"slurm"`.
- **Manifest**: `MANIFEST = PluginManifest(...)` declares the
  contributed surface for `hpc-agent capabilities`.
- **The hook map**: `backend.py` translates each `HPCBackend`
  capability hook (submit, alive-check, state classify, log fetch)
  into marketplace-API terms, including mapping instance interruption
  onto the host's existing `preempted` semantics.

## Try the wiring (registration only)

```bash
pip install -e examples/plugins/hpc-agent-vastai
python -c "
from hpc_agent._kernel.registry import plugins
plugins.load_plugins()  # entry-point scan
import hpc_agent_vastai.backend  # the import load_plugins triggers via primitive_modules
from hpc_agent.infra.backends import get_backend_class
print(get_backend_class('vastai'))
"
```

With the plugin installed, a `clusters.yaml` entry may set
`scheduler: vastai` directly (the host's config validator accepts any
plugin-registered backend name), and the submit flow constructs the
backend through `VastAIBackend.from_build_context` — the host's
construction seam — reading `$VAST_API_KEY` / `$HPC_VASTAI_IMAGE`
instead of the SSH fields. Submitting still fails by design
(`NotImplementedError`) until the platform API layer is implemented.
