"""``campaign-advance`` primitive — recommend the next campaign action.

Composes :func:`campaign_status`, :func:`campaign_converged`, and
:func:`campaign_budget` into a single decision: ``continue``,
``stop_converged``, ``stop_over_budget``, or ``wait_in_flight``.
The agent reads ``decision`` to choose its next move; ``reason`` is
human-readable.

Pure read. Stop criteria + budget caps come in as CLI args (no
manifest yet — see campaign/ discussion).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, get_args

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire._shared import OptimizationDirection, PlateauMode
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-advance",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Decide the next campaign action (continue / stop_converged / "
            "stop_over_budget / wait_in_flight)."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg("--campaign-id", type=str, required=True),
            CliArg("--max-iters", type=int, default=None),
            CliArg("--metric", type=str, default=None),
            CliArg("--target", type=float, default=None),
            CliArg(
                "--direction",
                type=str,
                default=None,
                choices=get_args(OptimizationDirection),
            ),
            CliArg("--plateau-window", type=int, default=None),
            CliArg("--plateau-tolerance", type=float, default=None),
            CliArg(
                "--plateau-mode",
                type=str,
                default=None,
                choices=get_args(PlateauMode),
                help="See ``campaign-converged --help``.",
            ),
            CliArg("--max-jobs", type=int, default=None),
            CliArg("--max-tasks", type=int, default=None),
            CliArg("--max-walltime-sec", type=int, default=None),
        ),
        group="campaign",
    ),
)
def campaign_advance(
    *,
    experiment_dir: Path,
    campaign_id: str,
    max_iters: int | None = None,
    metric: str | None = None,
    target: float | None = None,
    direction: OptimizationDirection | None = None,
    plateau_window: int | None = None,
    plateau_tolerance: float | None = None,
    plateau_mode: PlateauMode | None = None,
    max_jobs: int | None = None,
    max_tasks: int | None = None,
    max_walltime_sec: int | None = None,
) -> dict[str, Any]:
    """Decide the next campaign action from history + budget.

    Decision precedence:
      1. ``stop_over_budget`` — any supplied budget cap met
      2. ``stop_converged``   — any supplied stop criterion fired
      3. ``wait_in_flight``   — runs are still pending (let them finish first)
      4. ``continue``         — agent should plan the next iteration

    Returns the underlying ``status``, ``converged``, and ``budget``
    payloads so the agent can drill in without a second CLI call.
    """
    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction
    from hpc_agent.meta.campaign.atoms.budget import campaign_budget
    from hpc_agent.meta.campaign.atoms.converged import campaign_converged
    from hpc_agent.meta.campaign.atoms.status import campaign_status

    status = campaign_status(experiment_dir=experiment_dir, campaign_id=campaign_id)
    budget = campaign_budget(
        experiment_dir=experiment_dir,
        campaign_id=campaign_id,
        max_jobs=max_jobs,
        max_tasks=max_tasks,
        max_walltime_sec=max_walltime_sec,
    )
    converged = campaign_converged(
        experiment_dir=experiment_dir,
        campaign_id=campaign_id,
        max_iters=max_iters,
        metric=metric,
        target=target,
        direction=direction,
        plateau_window=plateau_window,
        plateau_tolerance=plateau_tolerance,
        plateau_mode=plateau_mode,
    )

    # The decision precedence is a total deterministic ladder over the three
    # computed payloads — express it as ordered kernel rules so the campaign
    # 'decide' point routes through the same evaluator as every other decision
    # point. The 'continue' catch-all is the default branch (this point never
    # escalates: every input lands on one of the four outcomes).
    evidence = {"status": status, "budget": budget, "converged": converged}

    def _over_budget(e: dict[str, Any]) -> CandidateAction | None:
        if e["budget"]["exhausted"]:
            return CandidateAction(action="stop_over_budget", rationale=e["budget"]["reason"])
        return None

    def _wait_in_flight(e: dict[str, Any]) -> CandidateAction | None:
        # In-flight runs must finish before a stop decision so we don't orphan
        # cluster jobs the campaign can't keep tracking.
        n = e["status"]["in_flight"]
        if n > 0:
            return CandidateAction(action="wait_in_flight", rationale=f"{n} run(s) still in flight")
        return None

    def _converged(e: dict[str, Any]) -> CandidateAction | None:
        if e["converged"]["converged"]:
            return CandidateAction(action="stop_converged", rationale=e["converged"]["reason"])
        return None

    outcome = decide(
        "decide",
        evidence,
        rules=[_over_budget, _wait_in_flight, _converged],
        default=CandidateAction(
            action="continue",
            rationale=(
                f"{status['iterations']} iteration(s) complete, no stop criterion met, "
                "no in-flight runs"
            ),
        ),
    )
    assert outcome.chosen is not None  # a total ladder always resolves to a branch

    return {
        "campaign_id": campaign_id,
        "decision": outcome.chosen.action,
        "reason": outcome.reason,
        "status": status,
        "converged": converged,
        "budget": budget,
    }
