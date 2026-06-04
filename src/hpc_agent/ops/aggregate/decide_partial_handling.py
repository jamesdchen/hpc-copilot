"""``decide-partial-handling`` primitive — proceed on incomplete waves or not.

The aggregate ``partial_handling`` point used to be prose: on an
``escalation_reason`` (some waves failed ``combiner_max_retries``), "the
caller decides whether the partial result is acceptable." Part of that is
a **switch on observable evidence**, not pure judgement:

* whether the combiner retries are spent is a code fact — if not, the fix
  is simply to *retry*, no decision needed;
* the *missing fraction* ``failed / (failed + combined)`` is a computed
  number, not a feeling.

So this primitive resolves the deterministic part in code
(``decided_by="code"``: ``retry`` while retries remain) and escalates only
the genuine residue once retries are exhausted: whether an *acceptable*
missing fraction is OK *for the experiment's purpose* — risk/intent the
framework cannot observe. The escalation carries the missing fraction as
evidence so the judgement is "accept this much loss or force-retry?", not
prose from scratch. Routes through the shared decision kernel.

Pure function over supplied evidence — no I/O, never raises
(``error_codes=[]``).
"""

from __future__ import annotations

from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = ["decide_partial_handling"]


@primitive(
    name="decide-partial-handling",
    verb="query",
    side_effects=[],
    error_codes=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Decide whether to proceed on incomplete aggregate waves from "
            "observable evidence: failed-wave count, combined-wave count, and "
            "whether combiner retries are exhausted. Resolves retry "
            "deterministically (decided_by=code) while retries remain; once "
            "exhausted, escalates the accept-partial-vs-force-retry choice "
            "(decided_by=judgement) carrying the computed missing fraction."
        ),
        args=(
            CliArg("--failed-count", type=int, required=True, help="Number of failed waves."),
            CliArg("--combined-count", type=int, required=True, help="Number of combined waves."),
            CliArg(
                "--retries-exhausted",
                action="store_true",
                help="Set when the failed waves have spent combiner_max_retries.",
            ),
        ),
    ),
    agent_facing=True,
)
def decide_partial_handling(
    *,
    failed_count: int,
    combined_count: int,
    retries_exhausted: bool = False,
) -> dict[str, Any]:
    """Decide retry vs. accept-partial from wave evidence.

    ``retry`` is the deterministic code branch while retries remain; once
    exhausted the accept-vs-force-retry choice escalates with the computed
    missing fraction as evidence.
    """
    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction, Escalation

    total = failed_count + combined_count
    missing_fraction = round(failed_count / total, 4) if total else 0.0

    def _none_failed(_: Any) -> CandidateAction | None:
        if failed_count == 0:
            return CandidateAction(action="proceed", source="policy", rationale="no failed waves")
        return None

    def _retry_eligible(_: Any) -> CandidateAction | None:
        if not retries_exhausted:
            return CandidateAction(
                action="retry",
                source="policy",
                rationale="combiner retries not yet exhausted — retry before deciding",
            )
        return None

    def _escalate(_: Any) -> Escalation:
        return Escalation(
            decided_by="judgement",
            reason=(
                f"{failed_count}/{total} wave(s) failed after retries exhausted "
                f"(missing {missing_fraction:.0%}) — accept the partial result or force-retry?"
            ),
            candidate_actions=[
                CandidateAction(
                    action="accept-partial",
                    source="policy",
                    params={"missing_fraction": missing_fraction},
                ),
                CandidateAction(action="force-retry-failed", source="policy"),
            ],
        )

    decision = decide(
        "partial_handling", None, rules=[_none_failed, _retry_eligible], on_abstain=_escalate
    )
    return {
        "decided_by": decision.decided_by,
        "decision": decision.chosen.action if decision.chosen is not None else None,
        "missing_fraction": missing_fraction,
        "failed_count": failed_count,
        "combined_count": combined_count,
        "reason": decision.reason,
        "candidates": (
            [c.action for c in decision.escalation.candidate_actions]
            if decision.escalation is not None
            else []
        ),
    }
