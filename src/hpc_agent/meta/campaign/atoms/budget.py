"""``campaign-budget`` primitive — track spent vs. supplied budget caps.

Pure read over sidecars tagged with *campaign_id*. Sums:

* ``jobs``         — number of completed run sidecars
* ``tasks``        — sum of ``task_count`` across completed runs
* ``walltime_sec`` — Σ per-task ``elapsed_sec`` joined from the
  runtime-prior store on ``run_id`` (see
  :mod:`hpc_agent.meta.campaign.atoms.compute_spend`)
* ``core_hours``   — Σ ``elapsed_sec × effective_cores`` / 3600
* ``gpu_hours``    — Σ GPU-task ``elapsed_sec`` / 3600

Budget caps come in as CLI args; the framework holds no opinion about
defaults. Returns ``exhausted=True`` if any supplied cap is met.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-budget",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help="Roll up campaign-level spend and compare to optional caps.",
        experiment_dir_arg=True,
        args=(
            CliArg("--campaign-id", type=str, required=True),
            CliArg("--max-jobs", type=int, default=None),
            CliArg("--max-tasks", type=int, default=None),
            CliArg("--max-walltime-sec", type=int, default=None),
            CliArg("--max-core-hours", type=float, default=None),
        ),
        group="campaign",
    ),
)
def campaign_budget(
    *,
    experiment_dir: Path,
    campaign_id: str,
    max_jobs: int | None = None,
    max_tasks: int | None = None,
    max_walltime_sec: int | None = None,
    max_core_hours: float | None = None,
) -> dict[str, Any]:
    """Roll up campaign-level spend and compare to optional caps.

    Returns ``{spent: {...}, budget: {...}, remaining: {...}, projected:
    {...}, coverage: {...}, exhausted: bool, reason: str}``. A ``None``
    cap means "untracked" and never triggers exhaustion.

    ``spent`` carries ``jobs`` / ``tasks`` (sidecar counts) plus the
    real compute consumption joined from the runtime-prior store on
    ``run_id``: ``walltime_sec`` (Σ per-task elapsed), ``core_hours``
    (Σ elapsed×cores/3600), and ``gpu_hours``. ``coverage`` reports
    which runs had no runtime samples (accounted as honest zeros, NOT a
    silent global zero) so the agent can see partial accounting.

    ``projected`` adds a best-effort estimate for in-flight runs that
    have not produced samples yet (per-task mean × in-flight task count);
    it is consumed + that estimate. It is advisory and never drives the
    ``exhausted`` decision — only realised spend can exhaust a cap.

    Manifest defaults: any cap left as ``None`` falls back to the
    matching field under ``budget`` in ``<campaign_dir>/manifest.json``
    if the manifest exists. Explicit CLI args always win.
    """
    import jsonschema

    from hpc_agent.execution.mapreduce.reduce.history import find_sidecars_by_campaign
    from hpc_agent.meta.campaign.atoms.compute_spend import consumed_compute_for_campaign
    from hpc_agent.meta.campaign.manifest import read_manifest
    from hpc_agent.state.index import find_runs_by_campaign

    manifest_budget: dict[str, Any] = {}
    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError):
        # A malformed manifest shouldn't crash budget reads, but a bare
        # `except Exception` would also swallow KeyboardInterrupt /
        # SystemExit. Narrow to the IO + parse errors we expect here.
        manifest = None
    if manifest is not None:
        manifest_budget = manifest.get("budget") or {}

    if max_jobs is None:
        max_jobs = manifest_budget.get("max_jobs")
    if max_tasks is None:
        max_tasks = manifest_budget.get("max_tasks")
    if max_walltime_sec is None:
        max_walltime_sec = manifest_budget.get("max_walltime_sec")
    if max_core_hours is None:
        max_core_hours = manifest_budget.get("max_core_hours")

    sidecars = find_sidecars_by_campaign(experiment_dir, campaign_id)
    spent_jobs = len(sidecars)
    spent_tasks = sum(int(s.get("task_count") or 0) for s in sidecars)

    consumed = consumed_compute_for_campaign(experiment_dir, sidecars)
    spent_walltime = consumed["walltime_sec"]
    spent_core_hours = consumed["core_hours"]

    spent = {
        "jobs": spent_jobs,
        "tasks": spent_tasks,
        "walltime_sec": int(spent_walltime),
        "core_hours": spent_core_hours,
        "gpu_hours": consumed["gpu_hours"],
    }
    budget = {
        "max_jobs": max_jobs,
        "max_tasks": max_tasks,
        "max_walltime_sec": max_walltime_sec,
        "max_core_hours": max_core_hours,
    }
    remaining: dict[str, int | float | None] = {}
    exhausted = False
    reasons: list[str] = []

    # Cap-vs-spend ladder. Integer caps (jobs/tasks/walltime) compare on
    # ints; max_core_hours is a float cap compared on the float core-hours.
    int_checks: tuple[tuple[str, int], ...] = (
        ("max_jobs", spent_jobs),
        ("max_tasks", spent_tasks),
        ("max_walltime_sec", int(spent_walltime)),
    )
    for key, spent_val in int_checks:
        cap = budget[key]
        if cap is None:
            remaining[key] = None
            continue
        cap_int = int(cap)
        remaining[key] = max(0, cap_int - spent_val)
        if spent_val >= cap_int:
            exhausted = True
            reasons.append(f"{key} ({spent_val} >= {cap_int})")

    if max_core_hours is None:
        remaining["max_core_hours"] = None
    else:
        cap_ch = float(max_core_hours)
        remaining["max_core_hours"] = round(max(0.0, cap_ch - spent_core_hours), 4)
        if spent_core_hours >= cap_ch:
            exhausted = True
            reasons.append(f"max_core_hours ({spent_core_hours} >= {cap_ch})")

    projected = _projected_spend(
        find_runs_by_campaign(experiment_dir, campaign_id),
        consumed=consumed,
        spent_walltime=int(spent_walltime),
        spent_core_hours=spent_core_hours,
    )

    return {
        "campaign_id": campaign_id,
        "spent": spent,
        "budget": budget,
        "remaining": remaining,
        "projected": projected,
        "coverage": consumed["coverage"],
        "exhausted": exhausted,
        "reason": "; ".join(reasons) if reasons else "within_budget",
    }


def _projected_spend(
    runs: list[Any],
    *,
    consumed: dict[str, Any],
    spent_walltime: int,
    spent_core_hours: float,
) -> dict[str, Any]:
    """Best-effort projection: consumed + an estimate for in-flight tasks.

    The only "remaining work" the framework can count without inventing
    state is the set of runs currently ``in_flight`` in the journal —
    they are consuming compute right now but have not yet contributed
    runtime-prior samples. We estimate their spend as
    ``per_task_mean × in_flight_task_count`` using the per-task mean from
    the *already-consumed* samples (the campaign's own observed rate).

    Future, not-yet-submitted iterations are NOT projected — their task
    counts don't exist anywhere yet, and fabricating a horizon would be
    inventing state the issue explicitly says to avoid. ``basis`` records
    this limitation so the agent reads the projection honestly.
    """
    tasks_counted = int(consumed.get("tasks_counted") or 0)
    in_flight_runs = [r for r in runs if getattr(r, "status", None) == "in_flight"]
    in_flight_tasks = sum(int(getattr(r, "total_tasks", 0) or 0) for r in in_flight_runs)

    if tasks_counted <= 0 or in_flight_tasks <= 0:
        # No observed per-task rate, or nothing in flight → projection is
        # just the consumed figure (an honest lower bound).
        return {
            "walltime_sec": int(spent_walltime),
            "core_hours": round(spent_core_hours, 4),
            "in_flight_runs": len(in_flight_runs),
            "in_flight_tasks": in_flight_tasks,
            "basis": (
                "consumed_only: no in-flight tasks or no observed per-task rate; "
                "future iterations are not projected (their task counts do not exist yet)"
            ),
        }

    mean_walltime = spent_walltime / tasks_counted
    mean_core_hours = spent_core_hours / tasks_counted
    return {
        "walltime_sec": int(spent_walltime + mean_walltime * in_flight_tasks),
        "core_hours": round(spent_core_hours + mean_core_hours * in_flight_tasks, 4),
        "in_flight_runs": len(in_flight_runs),
        "in_flight_tasks": in_flight_tasks,
        "basis": (
            "consumed + per_task_mean × in_flight_tasks; future (not-yet-submitted) "
            "iterations are not projected"
        ),
    }
