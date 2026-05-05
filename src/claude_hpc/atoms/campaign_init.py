"""``campaign-init`` primitive — write the campaign manifest.

Thin CLI wrapper around :func:`claude_hpc.campaign.manifest.write_manifest`.
The agent typically calls this once at campaign creation; later
primitives (``campaign-advance``, ``campaign-budget``,
``campaign-converged``) auto-default missing args from the manifest.

CLI args mirror the manifest fields exactly. ``strategy.params`` can
be supplied as a JSON string via ``--strategy-params-json``; if the
agent needs richer params it should call ``write_manifest`` directly
from Python rather than encode arbitrary JSON on the command line.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import SideEffect, primitive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-init",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/campaigns/<id>/manifest.json"),
    ],
    idempotent=True,
)
def campaign_init(
    *,
    experiment_dir: Path,
    campaign_id: str,
    goal: str = "",
    max_iters: int | None = None,
    metric: str | None = None,
    target: float | None = None,
    direction: str | None = None,
    plateau_window: int | None = None,
    plateau_tolerance: float | None = None,
    max_jobs: int | None = None,
    max_tasks: int | None = None,
    max_walltime_sec: int | None = None,
    strategy_name: str | None = None,
    strategy_params_json: str | None = None,
) -> dict[str, Any]:
    """Write ``<campaign_dir>/manifest.json`` from CLI args.

    Idempotent: re-running with the same args produces the same file
    (atomic-write semantics, no partial writes). Re-running with
    different args overwrites — the agent is expected to treat
    campaign-init as a one-shot at creation, not an in-flight mutator.
    """
    from claude_hpc.campaign.manifest import manifest_path, write_manifest

    budget: dict[str, Any] | None = None
    if any(v is not None for v in (max_jobs, max_tasks, max_walltime_sec)):
        budget = {
            "max_jobs": max_jobs,
            "max_tasks": max_tasks,
            "max_walltime_sec": max_walltime_sec,
        }

    stop_criteria: dict[str, Any] | None = None
    if any(
        v is not None
        for v in (max_iters, metric, target, direction, plateau_window, plateau_tolerance)
    ):
        stop_criteria = {}
        if max_iters is not None:
            stop_criteria["max_iters"] = max_iters
        if metric is not None:
            stop_criteria["metric"] = metric
        if target is not None:
            stop_criteria["target"] = target
        if direction is not None:
            stop_criteria["direction"] = direction
        if plateau_window is not None:
            stop_criteria["plateau_window"] = plateau_window
        if plateau_tolerance is not None:
            stop_criteria["plateau_tolerance"] = plateau_tolerance

    strategy: dict[str, Any] | None = None
    if strategy_name is not None:
        params: dict[str, Any] = {}
        if strategy_params_json:
            parsed = json.loads(strategy_params_json)
            if not isinstance(parsed, dict):
                raise ValueError("--strategy-params-json must decode to a JSON object")
            params = parsed
        strategy = {"name": strategy_name, "params": params}

    path = write_manifest(
        experiment_dir,
        campaign_id=campaign_id,
        goal=goal,
        budget=budget,
        stop_criteria=stop_criteria,
        strategy=strategy,
    )
    return {
        "campaign_id": campaign_id,
        "manifest_path": str(path),
        "manifest_path_relative": str(
            manifest_path(experiment_dir, campaign_id).relative_to(experiment_dir)
        ),
    }
