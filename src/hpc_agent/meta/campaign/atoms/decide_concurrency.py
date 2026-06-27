"""``decide-concurrency`` primitive — how many campaign iterations in flight.

The campaign ``concurrency`` point used to be prose the worker reasoned
("default sequential; opt into K-in-flight when the optimizer supports
it"). But most of that decision is a **switch on observable evidence**,
not judgement:

* whether the strategy *can* run async is a code fact —
  ``classify-campaign-path``'s ``supports_async_concurrency``;
* whether there is room to run more is a code fact —
  ``campaign-budget``'s ``remaining`` headroom minus what's in flight.

So this primitive resolves the deterministic majority in code
(``decided_by="code"``, ``sequential``) — the strategy isn't built for
parallel asks, or there's no budget headroom — and escalates only the
genuine residue: *how aggressively* to parallelize within the computed
safe bound (a risk-appetite call). The escalation carries that bound, so
the judgement is "pick K in [1, bound]", not "reason about concurrency
from scratch". Routes through the shared decision kernel.

Pure function over supplied evidence — no I/O, never raises
(``error_codes=[]``).
"""

from __future__ import annotations

from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.meta.campaign.atoms._concurrency import DEFAULT_MAX_IN_FLIGHT as _K_CAP_DEFAULT

__all__ = ["decide_concurrency"]


@primitive(
    name="decide-concurrency",
    verb="query",
    side_effects=[],
    error_codes=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Decide campaign iteration concurrency from observable evidence: "
            "the strategy's async support (classify-campaign-path's "
            "supports_async_concurrency) and budget headroom (campaign-budget's "
            "remaining minus in-flight). Resolves sequential deterministically "
            "(decided_by=code) when async is unsupported or there's no headroom; "
            "otherwise escalates the how-aggressive choice (decided_by=judgement) "
            "carrying the computed safe max_in_flight bound."
        ),
        args=(
            CliArg(
                "--supports-async",
                action="store_true",
                help="Set when the strategy is built for parallel asks (Optuna/constant_liar).",
            ),
            CliArg(
                "--remaining-jobs",
                type=int,
                default=None,
                help="Budget headroom in jobs (campaign-budget remaining); omit = unbounded.",
            ),
            CliArg("--in-flight", type=int, default=0, help="Iterations currently in flight."),
            CliArg(
                "--k-cap",
                type=int,
                default=_K_CAP_DEFAULT,
                help="Upper guardrail on parallelism regardless of headroom (default 4).",
            ),
        ),
    ),
    agent_facing=True,
)
def decide_concurrency(
    *,
    supports_async: bool = False,
    remaining_jobs: int | None = None,
    in_flight: int = 0,
    k_cap: int = _K_CAP_DEFAULT,
) -> dict[str, Any]:
    """Decide sequential vs. how-aggressive parallel from evidence.

    Sequential is the deterministic code branch (no async support, or no
    headroom); when both hold, the safe bound ``min(k_cap, headroom)`` is
    computed and the *how many* choice escalates.
    """
    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction, Escalation

    headroom = None if remaining_jobs is None else max(0, remaining_jobs - in_flight)
    safe_bound = k_cap if headroom is None else max(1, min(k_cap, headroom))

    def _not_async(_: Any) -> CandidateAction | None:
        if not supports_async:
            return CandidateAction(
                action="sequential",
                source="policy",
                rationale="strategy not built for parallel asks (constant_liar/optuna absent)",
            )
        return None

    def _no_headroom(_: Any) -> CandidateAction | None:
        if headroom is not None and headroom <= 0:
            return CandidateAction(
                action="sequential",
                source="policy",
                rationale=f"no budget headroom (remaining={remaining_jobs}, in_flight={in_flight})",
            )
        return None

    def _escalate(_: Any) -> Escalation:
        return Escalation(
            decided_by="judgement",
            reason=(
                f"strategy supports async and there is headroom; you may run up to "
                f"{safe_bound} iteration(s) in flight — choose how aggressive"
            ),
            candidate_actions=[
                CandidateAction(action="sequential", source="policy", rationale="safe default"),
                CandidateAction(
                    action="parallel",
                    source="policy",
                    params={"max_in_flight": safe_bound},
                    rationale=f"up to {safe_bound} in flight",
                ),
            ],
        )

    decision = decide("concurrency", None, rules=[_not_async, _no_headroom], on_abstain=_escalate)
    return {
        "decided_by": decision.decided_by,
        "decision": decision.chosen.action if decision.chosen is not None else None,
        "max_in_flight": 1 if decision.decided_by == "code" else safe_bound,
        "supports_async": bool(supports_async),
        "reason": decision.reason,
        "candidates": (
            [c.action for c in decision.escalation.candidate_actions]
            if decision.escalation is not None
            else []
        ),
    }
