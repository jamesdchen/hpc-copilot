"""``campaign-status`` primitive — read-only summary of a campaign."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.state.index import find_runs_by_campaign

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-status",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Report per-iteration reduced metrics for one campaign. "
            "Walks every sidecar tagged with --campaign-id, runs "
            "reduce_metrics on each, and emits the history dict-list."
        ),
        experiment_dir_arg=True,
        args=(CliArg("--campaign-id", type=str, required=True),),
        group="campaign",
    ),
    agent_facing=True,
)
def campaign_status(*, experiment_dir: Path, campaign_id: str) -> dict[str, Any]:
    """Return per-iteration history + in-flight count for a campaign.

    Walks every sidecar tagged with *campaign_id* and reports the
    per-iteration reduced metrics dicts (``history.prior``) plus an
    in-flight count (sidecars whose journal status is still
    ``in_flight``). No SSH, no scheduler — pure local filesystem read.
    """
    from hpc_agent.execution.mapreduce.reduce.history import find_sidecars_by_campaign, prior

    sidecars = find_sidecars_by_campaign(experiment_dir, campaign_id)
    history = prior(experiment_dir, campaign_id)
    in_flight_records = find_runs_by_campaign(experiment_dir, campaign_id)
    # Count every NON-TERMINAL outstanding run, not just ``in_flight``: a
    # ``submitting`` orphan (U3 live flip) can name a LIVE array whose id-read
    # was severed, so the wait/idle checks that consume this count must block on
    # it too rather than let the campaign stop it unmonitored (provenance-review
    # F2). ``submitting`` is absent from ``TERMINAL_STATUSES``.
    in_flight = sum(1 for r in in_flight_records if r.status not in TERMINAL_STATUSES)
    return {
        "campaign_id": campaign_id,
        "iterations": len(sidecars),
        "in_flight": in_flight,
        "history": history,
        "run_ids": [s.get("run_id", "") for s in sidecars],
    }
