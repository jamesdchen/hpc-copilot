"""hpc-agent-notebook-render — the jupytext EXPORT for the notebook-audit substrate.

The scheduled v1.5 export (``docs/design/notebook-audit.md``, "The audit SURFACE
— harness-first" + "THE HARNESS CONTRACT"; ``docs/internals/harness-contract.md``
"The second-conforming-harness sketch"). A projection over SEALED records (the
audited source ``.py`` + template + the decision journal), built in the
plugin/tools lane so jupytext NEVER enters ``src/hpc_agent`` (the
library-knowledge boundary — this plugin is out of every boundary lint's scope by
design).

Registration path, identical to the built-in plugins:

1. The ``hpc_agent.plugins`` entry point (``pyproject.toml``) resolves here.
2. The host imports every module in ``primitive_modules`` for its side effects;
   importing ``.render`` / ``.ingest`` runs their ``@primitive`` decorators, so
   ``notebook-render`` / ``notebook-ingest-signoffs`` register as ordinary CLI +
   MCP verbs with ZERO host edits (this plugin is the FIRST to register a
   ``@primitive``; the mechanism was built + manifest-linted, unexercised until
   now).
3. ``MANIFEST`` declares the two contributed verbs for ``hpc-agent capabilities``
   and the ``scripts/lint_plugin_manifests.py`` reconciliation gate.

Two roles, in order (the export's whole point):

* **The portability artifact** — an audit readable anywhere, with no harness:
  ``notebook-render`` writes a ``.ipynb`` whose per-section AUDIT CELLS carry the
  status / tier / shas the core view computed.
* **A SECOND CONFORMING HARNESS** — a human typing into a rendered sign-off cell
  is out-of-band from the LLM, so ``notebook-ingest-signoffs`` writes that typed
  text through the documented utterance-log write API
  (``state/utterances.py::append_utterance``) AND appends the sign-off via the
  core append-decision path — providing the full-strength authorship tier with NO
  Claude Code anywhere in the loop.
"""

from __future__ import annotations

from hpc_agent._wire.plugin_manifest import PluginManifest

__version__ = "0.1.0"

# Imported by the host for registration side effects — each module's
# ``@primitive`` decorator fires at import time.
primitive_modules = (
    "hpc_agent_notebook_render.render",
    "hpc_agent_notebook_render.ingest",
)

MANIFEST = PluginManifest(
    name="hpc-agent-notebook-render",
    version=__version__,
    primitives=("notebook-render", "notebook-ingest-signoffs"),
    worker_prompt_overlays=(),
    # No CLI subcommands wired by hand: the two verbs surface through their
    # ``@primitive`` CliShape via the host's registry walk (build_parser), so
    # there is no ``register_cli`` hook — cli_register stays False (honest wrt the
    # loader, which invokes register_cli only if present).
    cli_register=False,
)
