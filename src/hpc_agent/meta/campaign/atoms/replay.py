"""``campaign-replay`` primitive — last N iterations of a campaign.

Diagnostic: returns each iteration's sidecar metadata and reduced
metrics so the agent can sanity-check what the strategy actually did
across recent steps. Pure read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-replay",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help="Return the last N iterations of a campaign with reduced metrics.",
        experiment_dir_arg=True,
        args=(
            CliArg("--campaign-id", type=str, required=True),
            CliArg("--last-n", type=int, default=5),
        ),
        group="campaign",
    ),
)
def campaign_replay(
    *,
    experiment_dir: Path,
    campaign_id: str,
    last_n: int = 5,
) -> dict[str, Any]:
    """Return the last *last_n* iterations of *campaign_id*, oldest-first.

    Each iteration carries the sidecar's ``run_id``, ``submitted_at``,
    ``status``, and the reduced metrics dict produced by
    :func:`mapreduce.reduce.history.prior`. Iterations whose result
    directories don't exist yet (still in flight) carry an empty
    metrics dict.
    """
    from hpc_agent.models.mapreduce.reduce.history import find_sidecars_by_campaign, prior

    n = int(last_n)
    if n < 0:
        raise errors.SpecInvalid("last_n must be >= 0")
    sidecars = find_sidecars_by_campaign(experiment_dir, campaign_id)
    iterations: list[dict[str, Any]] = []
    if n == 0:
        return {
            "campaign_id": campaign_id,
            "total_iterations": len(sidecars),
            "returned": 0,
            "iterations": iterations,
        }
    history = prior(experiment_dir, campaign_id)
    sliced_sidecars = sidecars[-n:]
    sliced_history = history[-n:]
    for sidecar, metrics in zip(sliced_sidecars, sliced_history, strict=False):
        iterations.append(
            {
                "run_id": sidecar.get("run_id", ""),
                "submitted_at": sidecar.get("submitted_at", ""),
                "status": sidecar.get("status", ""),
                "metrics": metrics,
            }
        )
    return {
        "campaign_id": campaign_id,
        "total_iterations": len(sidecars),
        "returned": len(iterations),
        "iterations": iterations,
    }
