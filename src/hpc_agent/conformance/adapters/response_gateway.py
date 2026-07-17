"""The response-gateway reference adapter (Wave C / T6) — capability 2, ACT, NO hooks.

The SECOND conforming shape of capability 2's ACT half
(``docs/internals/harness-contract.md``, "Capability 2, split: INSPECT vs ACT",
implementation 2): **a RESPONSE GATEWAY** — an LLM proxy that applies
``verify_relay`` to the outgoing message BEFORE delivery, holding back a
contradicted relay. The gateway sits in front of ANY model, so it provides the
ACT half with no Claude-Code ``Stop`` hook, no transcript, and no
``stop_hook_active`` protocol — it proves capability 2 is implementable outside
Claude Code's hook model.

* :meth:`ResponseGatewayAdapter.run_enforcement_point` runs the pre-delivery
  audit (:func:`hpc_agent.conformance.adapters._relay_audit_core.audit_final_message`,
  the SAME public ``verify_relay`` the Stop hook drives) over the outgoing
  *final_message* and BLOCKS delivery on a contradiction. Loop-safety is the
  gateway analogue of ``stop_hook_active``: a message already held back once
  (``previously_blocked=True``) is delivered — the gateway holds at most once,
  never wedges a session.
* :meth:`ResponseGatewayAdapter.detect_capabilities` detects relay-enforcement
  BY BEHAVIOR (the non-Claude-Code honest-detection rule, D-K3): it seeds a known
  state-contradiction and confirms its own gate holds the contradicting message
  back — the ACT seam proving its blocking invariant, never a hook needle.

It declares NOTHING else: no utterance log, no backgrounding — the kit SKIPS
those with their contract-named degraded tiers and the report reads
``partial: relay-enforcement``. Partial is honest, not a failure.

Stdlib + hpc-agent PUBLIC surface only; pytest-free (the D-K1 boundary). Loadable
via ``--harness-adapter hpc_agent.conformance.adapters.response_gateway:build``.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.conformance.adapter import CAP_RELAY_ENFORCEMENT, EnforcementOutcome
from hpc_agent.conformance.adapters._relay_audit_core import audit_final_message

__all__ = ["ResponseGatewayAdapter", "build"]


class ResponseGatewayAdapter:
    """A response-gateway harness behind the kit's adapter seam — ACT, no hooks."""

    name = "response-gateway"

    def run_enforcement_point(
        self, experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        """Gate *final_message* through ``verify_relay`` BEFORE delivery.

        The gateway ACT shape: a contradicting relay is HELD BACK (blocked) with
        the itemized mismatch summary so the agent must correct it; a faithful one
        is delivered. ``previously_blocked=True`` models a message the gateway
        already held once — it is delivered now (block AT MOST ONCE, the
        ``stop_hook_active`` analogue), so the gateway can never hard-block a turn.
        """
        if previously_blocked:
            return EnforcementOutcome(blocked=False, reason=None)
        verdict = audit_final_message(Path(experiment_dir), final_message)
        if verdict.contradicted:
            return EnforcementOutcome(blocked=True, reason=verdict.reason)
        return EnforcementOutcome(blocked=False, reason=None)

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        """Detect relay-enforcement BY BEHAVIOR — the non-Claude-Code honest rule.

        The gateway has no hook needle for ``harness-capabilities`` to probe, so it
        proves the capability by BEHAVING it (D-K3): it seeds the kit's known
        ``run_state_contradiction`` triple into *experiment_dir*'s journal and
        confirms its own gate holds the contradicting message back. A gate that
        blocks the seeded contradiction detects ``relay-enforcement`` and nothing
        else — utterance-log and backgrounding are genuinely absent.
        """
        from hpc_agent.conformance.relay_fixtures import load_triples, seed_triple

        experiment_dir = Path(experiment_dir)
        triple = next(t for t in load_triples() if t.name == "run_state_contradiction")
        seed_triple(experiment_dir, triple)
        if self.run_enforcement_point(experiment_dir, triple.final_message).blocked:
            return frozenset({CAP_RELAY_ENFORCEMENT})
        return frozenset()


def build() -> ResponseGatewayAdapter:
    """Zero-arg factory for ``--harness-adapter …adapters.response_gateway:build``."""
    return ResponseGatewayAdapter()
