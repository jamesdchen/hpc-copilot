"""``campaign-replay`` primitive — last N iterations of a campaign.

Diagnostic: returns each iteration's sidecar metadata and reduced
metrics so the agent can sanity-check what the strategy actually did
across recent steps. Pure read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_hpc._internal.primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-replay",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-mapreduce campaign-replay",
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
    from claude_hpc.mapreduce.reduce.history import find_sidecars_by_campaign, prior

    sidecars = find_sidecars_by_campaign(experiment_dir, campaign_id)
    history = prior(experiment_dir, campaign_id)
    n = max(1, int(last_n))
    sliced_sidecars = sidecars[-n:]
    sliced_history = history[-n:]
    iterations: list[dict[str, Any]] = []
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
