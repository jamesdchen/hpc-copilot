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

from typing import TYPE_CHECKING, Any, Literal

from hpc_agent._kernel.registry.primitive import primitive
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
                choices=("minimize", "maximize"),
            ),
            CliArg("--plateau-window", type=int, default=None),
            CliArg("--plateau-tolerance", type=float, default=None),
            CliArg(
                "--plateau-mode",
                type=str,
                default=None,
                choices=("prior_window", "all_time_best"),
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
    direction: Literal["minimize", "maximize"] | None = None,
    plateau_window: int | None = None,
    plateau_tolerance: float | None = None,
    plateau_mode: Literal["prior_window", "all_time_best"] | None = None,
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

    if budget["exhausted"]:
        decision = "stop_over_budget"
        reason = budget["reason"]
    elif status["in_flight"] > 0:
        # In-flight runs must finish before a stop decision so we don't
        # orphan cluster jobs the campaign can't keep tracking.
        decision = "wait_in_flight"
        reason = f"{status['in_flight']} run(s) still in flight"
    elif converged["converged"]:
        decision = "stop_converged"
        reason = converged["reason"]
    else:
        decision = "continue"
        reason = (
            f"{status['iterations']} iteration(s) complete, no stop criterion met, "
            "no in-flight runs"
        )

    return {
        "campaign_id": campaign_id,
        "decision": decision,
        "reason": reason,
        "status": status,
        "converged": converged,
        "budget": budget,
    }
