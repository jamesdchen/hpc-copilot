"""Reference harness adapters the conformance kit certifies against ITSELF (K8).

Two shipped adapters, each a :class:`~hpc_agent.conformance.adapter.HarnessAdapter`
loadable via ``--harness-adapter``:

* :mod:`~hpc_agent.conformance.adapters.claude_code` — the FULL reference. Drives
  hpc-agent's own hook cores IN-PROCESS as the harness (no live Claude Code, no
  network): capability 1 via the ``UserPromptSubmit`` / ``AskUserQuestion``
  capture cores, capability 2 via the relay-audit ``Stop`` seam, capability 3 via
  the kit's stub detached worker. It declares (and passes) all three — the
  self-conformance leg that pins our own side of the contract in CI.
* :mod:`~hpc_agent.conformance.adapters.notebook_render` — the SECOND harness, an
  honestly PARTIAL adapter: capability 1 only, via the jupytext render + notebook
  ``ingest-signoffs`` path. Relay enforcement and backgrounding are genuinely
  absent — the kit SKIPS them with the contract-named degraded tier and reports
  ``partial: utterance-log``.

Both are stdlib + hpc-agent core (claude_code) or a lazily-imported render stack
(notebook_render); neither imports pytest — the kit's pytest-free boundary (D-K1).
"""

from __future__ import annotations

__all__ = ["claude_code", "notebook_render"]
