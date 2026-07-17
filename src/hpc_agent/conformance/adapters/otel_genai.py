"""The OTel-GenAI INSPECT reference adapter (Wave C / T7) — capability 2, INSPECT only.

The observability-only conforming shape of capability 2's INSPECT half
(``docs/internals/harness-contract.md``, "Capability 2, split: INSPECT vs ACT"):
a harness that emits **OpenTelemetry GenAI** semantic-convention telemetry exposes
the final agent-visible message as an observable output the audit can READ — with
no Claude-Code hook — and REPORTS a relay contradiction as a span event. It is the
INSPECT half WITHOUT the ACT half: it OBSERVES and REPORTS, it never forces a
continuation, so it can SEE a contradiction but cannot stop it reaching the human.

This is the honesty the kit exists to keep: an INSPECT harness must be recorded at
the WEAKER, disclosed tier, never rounded up to a false pass of the ACT bar. So
this adapter DECLARES ``inspect_relay`` (the weaker
:data:`~hpc_agent.conformance.adapter.CAP_RELAY_INSPECT` tier) and DOES NOT declare
``run_enforcement_point`` (the ACT bar
:data:`~hpc_agent.conformance.adapter.CAP_RELAY_ENFORCEMENT`). Driven through the
full kit it earns ``partial: relay-inspection`` WITH ``relay-enforcement`` SKIPPED
at its ``verb-only relay-audit posture`` tier — INSPECT credited, ACT honestly
absent, the enforcement guarantee correctly still degraded.

* :meth:`OtelGenAiAdapter.inspect_relay` runs the SAME pre-delivery audit the
  gateway runs (:func:`hpc_agent.conformance.adapters._relay_audit_core.audit_final_message`,
  the public ``verify_relay``) and, whatever the verdict, EMITS a GenAI-conformant
  span event onto its in-process telemetry stream (:attr:`OtelGenAiAdapter.spans`),
  then RETURNS the observe-and-report verdict. It never blocks — there is no
  enforcement point to call.
* :meth:`OtelGenAiAdapter.detect_capabilities` returns the EMPTY set among the
  three contract nouns: INSPECT is not one of the three core seams (utterance-log
  / relay-enforcement / backgrounding), so an observe-only telemetry harness
  detects none of them — honest, and the negotiation seam checks agree (it
  declares no SEAM capability, so it detects none).

Stdlib + hpc-agent PUBLIC surface only; pytest-free (the D-K1 boundary). No real
OTel SDK dependency — the GenAI span shape is modeled in-process as the portable
observable-output contract, exactly as the notebook-render adapter models its
render path without a live Jupyter server. Loadable via
``--harness-adapter hpc_agent.conformance.adapters.otel_genai:build``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent.conformance.adapter import InspectionOutcome
from hpc_agent.conformance.adapters._relay_audit_core import audit_final_message

__all__ = ["OtelGenAiAdapter", "build"]

# The OpenTelemetry GenAI semantic-convention span name for an LLM turn's
# evaluation of its own output. The final message is an observable output on this
# span; a relay contradiction is reported as an event attribute. Modeled, not
# emitted through a live SDK — the shape is the portable contract.
_GENAI_SPAN_NAME = "gen_ai.evaluate_relay"
_MAX_OBSERVED_MESSAGE_CHARS = 4096  # bound the recorded output attribute


class OtelGenAiAdapter:
    """An OTel-GenAI telemetry harness behind the kit's adapter seam — INSPECT only.

    Declares the weaker :data:`CAP_RELAY_INSPECT` tier (by implementing
    ``inspect_relay``) and nothing else. The emitted spans accumulate on
    :attr:`spans` so a caller can confirm the harness DISCLOSED (reported via
    telemetry) rather than ENFORCED.
    """

    name = "otel-genai"

    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def inspect_relay(self, experiment_dir: Path, final_message: str) -> InspectionOutcome:
        """OBSERVE *final_message* and REPORT a contradiction as a GenAI span event.

        Runs the pre-delivery audit (the same public ``verify_relay`` the gateway
        and the Stop hook run), records a GenAI-conformant span carrying the final
        message as an observable output plus the contradiction verdict, and returns
        the observe-and-report outcome. It NEVER forces a continuation — the INSPECT
        half sees the contradiction; the enforcement guarantee stays verb-only.
        """
        verdict = audit_final_message(Path(experiment_dir), final_message)
        span = {
            "name": _GENAI_SPAN_NAME,
            "attributes": {
                # OTel GenAI observable-output convention: the final agent-visible
                # message is readable from the span, no Claude-Code hook involved.
                "gen_ai.response.final_message": final_message[:_MAX_OBSERVED_MESSAGE_CHARS],
                "gen_ai.evaluation.relay.contradicted": verdict.contradicted,
                "gen_ai.evaluation.relay.contradiction_kinds": list(verdict.kinds),
            },
            "events": (
                [{"name": "gen_ai.relay.contradiction", "attributes": {"detail": verdict.reason}}]
                if verdict.contradicted
                else []
            ),
        }
        self.spans.append(span)
        report = verdict.reason if verdict.contradicted else None
        return InspectionOutcome(detected=verdict.contradicted, report=report)

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:  # noqa: ARG002
        """Detect NONE of the three contract seams — INSPECT is not one of them.

        An observe-only telemetry harness provides no utterance-log, no
        relay-ENFORCEMENT seam, and no per-harness backgrounding seam, so it
        detects none of the three contract nouns. The weaker relay-inspection tier
        is declared (by ``inspect_relay``) and behaved (the kit's inspect module),
        but it is NOT a negotiation SEAM — so the honest detected set is empty.
        """
        return frozenset()


def build() -> OtelGenAiAdapter:
    """Zero-arg factory for ``--harness-adapter …adapters.otel_genai:build``."""
    return OtelGenAiAdapter()
