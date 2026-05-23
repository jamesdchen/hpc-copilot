"""``campaign-init`` primitive — write the campaign manifest.

Thin CLI wrapper around :func:`hpc_agent.campaign.manifest.write_manifest`.
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

from hpc_agent import errors
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-init",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/campaigns/<id>/manifest.json"),
    ],
    idempotent=True,
    idempotency_key="campaign_id",
    error_codes=[errors.SpecInvalid],
    cli=CliShape(
        help="Write the campaign manifest from CLI args.",
        experiment_dir_arg=True,
        args=(
            CliArg("--campaign-id", type=str, required=True),
            CliArg("--goal", type=str, default=""),
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
                help=(
                    "Plateau baseline (default ``all_time_best``). Controls whether the "
                    "recent window is compared to the all-time prior best or to the "
                    "prior window of equal size — see ``campaign-converged --help``."
                ),
            ),
            CliArg("--max-jobs", type=int, default=None),
            CliArg("--max-tasks", type=int, default=None),
            CliArg("--max-walltime-sec", type=int, default=None),
            CliArg("--strategy-name", type=str, default=None),
            CliArg(
                "--strategy-params-json",
                type=str,
                default=None,
                help="JSON object for strategy.params (round-tripped untouched).",
            ),
        ),
        group="campaign",
    ),
    agent_facing=True,
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
    plateau_mode: str | None = None,
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
    from hpc_agent.campaign.manifest import manifest_path, write_manifest

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
        for v in (
            max_iters,
            metric,
            target,
            direction,
            plateau_window,
            plateau_tolerance,
            plateau_mode,
        )
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
        if plateau_mode is not None:
            stop_criteria["plateau_mode"] = plateau_mode

    strategy: dict[str, Any] | None = None
    if strategy_name is not None:
        params: dict[str, Any] = {}
        if strategy_params_json:
            try:
                parsed = json.loads(strategy_params_json)
            except ValueError as exc:
                raise errors.SpecInvalid(
                    f"--strategy-params-json is not valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise errors.SpecInvalid("--strategy-params-json must decode to a JSON object")
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
