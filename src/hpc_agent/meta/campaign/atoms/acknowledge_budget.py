"""``campaign-acknowledge-budget`` primitive — clear a budget halt.

The budget governor (#224) turns ``stop_over_budget`` into a halt the
campaign loop cannot silently pass: once realised spend meets a cap,
``campaign-advance`` keeps halting until the spend is **explicitly
acknowledged**. This primitive is that acknowledgement.

It snapshots the campaign's current realised spend into
``<campaign_dir>/budget_ack.json``. Because spend is monotonic, the ack
authorises continuing only while spend stays at that snapshot — the next
task that burns compute re-arms the halt (a bare ack buys exactly one
more leg, not an open-ended bypass). Pass raised caps
(``--max-core-hours`` etc.) to also enlarge the budget in the same,
audited gesture: the new caps are written through to the manifest so
``campaign-advance`` reads real headroom on the next tick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path

_RAISE_CAP_KEYS: tuple[str, ...] = (
    "max_jobs",
    "max_tasks",
    "max_walltime_sec",
    "max_core_hours",
)


@primitive(
    name="campaign-acknowledge-budget",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/campaigns/<id>/budget_ack.json"),
    ],
    idempotent=True,
    idempotency_key="campaign_id",
    cli=CliShape(
        help=(
            "Acknowledge a campaign budget halt so the loop may continue. "
            "Snapshots current spend; optionally raises caps (written through "
            "to the manifest)."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg("--campaign-id", type=str, required=True),
            CliArg("--note", type=str, default=""),
            CliArg(
                "--max-jobs",
                type=int,
                default=None,
                help="Raise the manifest max_jobs cap in the same gesture.",
            ),
            CliArg("--max-tasks", type=int, default=None, help="Raise the max_tasks cap."),
            CliArg(
                "--max-walltime-sec",
                type=int,
                default=None,
                help="Raise the max_walltime_sec cap.",
            ),
            CliArg(
                "--max-core-hours",
                type=float,
                default=None,
                help="Raise the max_core_hours cap.",
            ),
        ),
        group="campaign",
    ),
    agent_facing=True,
)
def campaign_acknowledge_budget(
    *,
    experiment_dir: Path,
    campaign_id: str,
    note: str = "",
    max_jobs: int | None = None,
    max_tasks: int | None = None,
    max_walltime_sec: int | None = None,
    max_core_hours: float | None = None,
) -> dict[str, Any]:
    """Acknowledge the current budget halt; return what was acknowledged.

    Snapshots ``campaign-budget``'s realised ``spent`` block into the ack
    record. When raised caps are supplied they are merged into the
    manifest's ``budget`` section (existing caps and every other manifest
    section are preserved). Returns ``{campaign_id, acknowledged_spend,
    was_over_budget, raised_caps, ack_path}``.
    """
    from hpc_agent.meta.campaign.atoms.budget import campaign_budget
    from hpc_agent.meta.campaign.budget_ack import write_budget_ack

    raised_caps: dict[str, Any] = {
        "max_jobs": max_jobs,
        "max_tasks": max_tasks,
        "max_walltime_sec": max_walltime_sec,
        "max_core_hours": max_core_hours,
    }
    raised_caps = {k: v for k, v in raised_caps.items() if v is not None}

    if raised_caps:
        _raise_manifest_caps(experiment_dir, campaign_id, raised_caps)

    # Read budget AFTER raising caps so was_over_budget reflects the new
    # ceiling — raising the cap above current spend clears the halt outright.
    budget = campaign_budget(experiment_dir=experiment_dir, campaign_id=campaign_id)
    spent = budget["spent"]

    ack_file = write_budget_ack(
        experiment_dir,
        campaign_id=campaign_id,
        acknowledged_spend=spent,
        raised_caps=raised_caps or None,
        note=note,
    )

    return {
        "campaign_id": campaign_id,
        "acknowledged_spend": spent,
        "was_over_budget": budget["exhausted"],
        "raised_caps": raised_caps or None,
        "ack_path": str(ack_file),
    }


def _raise_manifest_caps(
    experiment_dir: Path, campaign_id: str, raised_caps: dict[str, Any]
) -> None:
    """Merge *raised_caps* into the manifest's ``budget`` section.

    Preserves every other section (goal, stop_criteria, strategy) and the
    existing budget caps not being raised. Creates a minimal manifest if
    none exists yet so the new caps are durable across ticks.
    """
    import json

    import jsonschema

    from hpc_agent.meta.campaign.manifest import read_manifest, write_manifest

    try:
        manifest = read_manifest(experiment_dir, campaign_id) or {}
    except (OSError, json.JSONDecodeError, jsonschema.ValidationError):
        # A schema-invalid / malformed manifest must not block clearing a
        # budget halt — degrade to no existing manifest and write fresh caps.
        manifest = {}
    budget = dict(manifest.get("budget") or {})
    budget.update(raised_caps)

    strategy = manifest.get("strategy")
    write_manifest(
        experiment_dir,
        campaign_id=campaign_id,
        goal=manifest.get("goal") or "",
        budget=budget,
        stop_criteria=manifest.get("stop_criteria"),
        strategy=strategy if isinstance(strategy, dict) else None,
        created_at=manifest.get("created_at"),
    )
