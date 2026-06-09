"""``campaign-advance`` primitive — recommend the next campaign action.

Composes :func:`campaign_status`, :func:`campaign_converged`, and
:func:`campaign_budget` into a single decision: ``continue``,
``stop_converged``, ``stop_over_budget``, ``stop_circuit_breaker``, or
``wait_in_flight``. The agent reads ``decision`` to choose its next move;
``reason`` is human-readable.

Pure read. Stop criteria + budget caps come in as CLI args (defaulting
from the manifest when omitted).
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
            "stop_over_budget / stop_circuit_breaker / wait_in_flight)."
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
            CliArg("--max-core-hours", type=float, default=None),
            CliArg(
                "--circuit-breaker-failures",
                type=int,
                default=None,
                help=(
                    "Loop-safety halt: stop the campaign when this many of the "
                    "most recent iterations failed consecutively (terminal "
                    "failed/abandoned runs, in submit order). No framework "
                    "default — omitted means no breaker."
                ),
            ),
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
    max_core_hours: float | None = None,
    circuit_breaker_failures: int | None = None,
) -> dict[str, Any]:
    """Decide the next campaign action from history + budget.

    Decision precedence:
      1. ``stop_over_budget``     — any supplied budget cap met
      2. ``wait_in_flight``       — runs are still pending (let them finish)
      3. ``stop_circuit_breaker`` — N most-recent iterations failed in a row
      4. ``stop_converged``       — any supplied stop criterion fired
      5. ``continue``             — agent should plan the next iteration

    The circuit breaker sits *after* ``wait_in_flight`` so an in-flight
    retry (which carries no terminal verdict yet) is given the chance to
    succeed before the loop-safety halt fires.

    Returns the underlying ``status``, ``converged``, ``budget``, and
    ``circuit_breaker`` payloads so the agent can drill in without a
    second CLI call.

    Manifest defaulting: ``circuit_breaker_failures`` falls back to the
    manifest's ``stop_criteria.circuit_breaker_failures`` when omitted,
    matching the budget/convergence caps.
    """
    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction
    from hpc_agent.meta.campaign.atoms.budget import campaign_budget
    from hpc_agent.meta.campaign.atoms.circuit_breaker import consecutive_terminal_failures
    from hpc_agent.meta.campaign.atoms.converged import campaign_converged
    from hpc_agent.meta.campaign.atoms.status import campaign_status
    from hpc_agent.state.index import find_runs_by_campaign

    if circuit_breaker_failures is None:
        circuit_breaker_failures = _manifest_circuit_breaker_failures(experiment_dir, campaign_id)

    status = campaign_status(experiment_dir=experiment_dir, campaign_id=campaign_id)
    budget = campaign_budget(
        experiment_dir=experiment_dir,
        campaign_id=campaign_id,
        max_jobs=max_jobs,
        max_tasks=max_tasks,
        max_walltime_sec=max_walltime_sec,
        max_core_hours=max_core_hours,
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
    breaker = consecutive_terminal_failures(find_runs_by_campaign(experiment_dir, campaign_id))
    breaker = {**breaker, "threshold": circuit_breaker_failures}

    # The decision precedence is a total deterministic ladder over the
    # computed payloads — express it as ordered kernel rules so the campaign
    # 'decide' point routes through the same evaluator as every other decision
    # point. The 'continue' catch-all is the default branch (this point never
    # escalates: every input lands on one of the outcomes).
    evidence = {
        "status": status,
        "budget": budget,
        "converged": converged,
        "breaker": breaker,
    }

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

    def _circuit_breaker(e: dict[str, Any]) -> CandidateAction | None:
        b = e["breaker"]
        threshold = b["threshold"]
        if threshold is not None and threshold > 0 and b["count"] >= threshold:
            return CandidateAction(
                action="stop_circuit_breaker",
                rationale=(
                    f"{b['count']} consecutive iteration failure(s) "
                    f">= circuit_breaker_failures ({threshold}); "
                    f"failing runs (newest-first): {b['run_ids']}"
                ),
            )
        return None

    def _converged(e: dict[str, Any]) -> CandidateAction | None:
        if e["converged"]["converged"]:
            return CandidateAction(action="stop_converged", rationale=e["converged"]["reason"])
        return None

    outcome = decide(
        "decide",
        evidence,
        rules=[_over_budget, _wait_in_flight, _circuit_breaker, _converged],
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
        "circuit_breaker": breaker,
    }


def _manifest_circuit_breaker_failures(experiment_dir: Path, campaign_id: str) -> int | None:
    """Read ``stop_criteria.circuit_breaker_failures`` from the manifest.

    Mirrors how :func:`campaign_budget` / :func:`campaign_converged`
    default their caps from the manifest. A missing / malformed manifest
    yields ``None`` (no breaker) rather than crashing the advance read.
    """
    import json

    from hpc_agent.meta.campaign.manifest import read_manifest

    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if manifest is None:
        return None
    stop_criteria = manifest.get("stop_criteria") or {}
    value = stop_criteria.get("circuit_breaker_failures")
    return value if isinstance(value, int) else None
