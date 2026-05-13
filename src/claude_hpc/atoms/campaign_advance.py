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

from claude_hpc._internal.primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-advance",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-agent campaign advance --campaign-id <id>",
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
    from claude_hpc.atoms.campaign_budget import campaign_budget
    from claude_hpc.atoms.campaign_converged import campaign_converged
    from claude_hpc.atoms.campaign_status import campaign_status

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
