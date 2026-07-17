"""Reference harness adapters the conformance kit certifies against ITSELF (K8).

Shipped adapters, each a :class:`~hpc_agent.conformance.adapter.HarnessAdapter`
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

The Wave-C second-harness proofs — NON-Claude conforming shapes that burn down the
risk register (``docs/plans/anti-vendor-lockout-2026-07-17.md`` §2/Wave C), each
honestly PARTIAL:

* :mod:`~hpc_agent.conformance.adapters.response_gateway` — capability 2's ACT half
  via a RESPONSE GATEWAY (``verify_relay`` pre-delivery), no Stop hook. Reports
  ``partial: relay-enforcement`` — proves the ACT bar is implementable outside the
  hook model.
* :mod:`~hpc_agent.conformance.adapters.otel_genai` — capability 2's INSPECT half
  via an OTel-GenAI telemetry stream (observe + report, never enforce). Reports
  ``partial: relay-inspection`` with ``relay-enforcement`` SKIPPED at its verb-only
  tier — the honest WEAKER tier, never a false ACT pass.
* :mod:`~hpc_agent.conformance.adapters.foreign_backgrounding` — capability 3 via a
  plain OS subprocess detach/wake, no Claude machinery. Reports
  ``partial: backgrounding``.

All are stdlib + hpc-agent core (claude_code / response_gateway / otel_genai /
foreign_backgrounding) or a lazily-imported render stack (notebook_render); none
imports pytest — the kit's pytest-free boundary (D-K1).
"""

from __future__ import annotations

__all__ = [
    "claude_code",
    "foreign_backgrounding",
    "notebook_render",
    "otel_genai",
    "response_gateway",
]
