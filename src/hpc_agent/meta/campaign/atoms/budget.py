"""``campaign-budget`` primitive — track spent vs. supplied budget caps.

Pure read over sidecars tagged with *campaign_id*. Sums:

* ``jobs``         — number of completed run sidecars
* ``tasks``        — sum of ``task_count`` across completed runs
* ``walltime_sec`` — sum of per-task elapsed times if observable

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


def _spent_walltime_sec(sidecar: dict[str, Any]) -> float:
    """Per-task elapsed_sec lives on the per-run journal RunRecord's
    ``last_status['summary']`` (counts only, no elapsed) and on the
    runtime-prior samples at ``.hpc/runtimes/<profile>__<cluster>.json``
    (full per-task elapsed). The previous implementation read
    ``sidecar.get('last_status')['tasks']`` — a key the sidecar never
    carries — and silently returned 0 on every call, so the
    ``max_walltime_sec`` budget cap was never enforced.

    Returning 0 explicitly here is honest until either the journal
    grows per-task elapsed or this function is rewritten to walk
    runtime-prior samples joined on run_id. Either way is a feature,
    not a quick fix.
    """
    return 0.0


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
) -> dict[str, Any]:
    """Roll up campaign-level spend and compare to optional caps.

    Returns ``{spent: {...}, budget: {...}, remaining: {...}, exhausted: bool, reason: str}``.
    A ``None`` cap means "untracked" and never triggers exhaustion.

    Manifest defaults: any cap left as ``None`` falls back to the
    matching field under ``budget`` in ``<campaign_dir>/manifest.json``
    if the manifest exists. Explicit CLI args always win.
    """
    from hpc_agent.meta.campaign.manifest import read_manifest
    from hpc_agent.models.mapreduce.reduce.history import find_sidecars_by_campaign

    manifest_budget: dict[str, Any] = {}
    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError):
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

    sidecars = find_sidecars_by_campaign(experiment_dir, campaign_id)
    spent_jobs = len(sidecars)
    spent_tasks = sum(int(s.get("task_count") or 0) for s in sidecars)
    spent_walltime = sum(_spent_walltime_sec(s) for s in sidecars)

    spent = {
        "jobs": spent_jobs,
        "tasks": spent_tasks,
        "walltime_sec": int(spent_walltime),
    }
    budget = {
        "max_jobs": max_jobs,
        "max_tasks": max_tasks,
        "max_walltime_sec": max_walltime_sec,
    }
    remaining: dict[str, int | None] = {}
    exhausted = False
    reasons: list[str] = []

    for key, spent_val in (
        ("max_jobs", spent_jobs),
        ("max_tasks", spent_tasks),
        ("max_walltime_sec", int(spent_walltime)),
    ):
        cap = budget[key]
        if cap is None:
            remaining[key] = None
            continue
        cap_int = int(cap)
        rem = max(0, cap_int - spent_val)
        remaining[key] = rem
        if spent_val >= cap_int:
            exhausted = True
            reasons.append(f"{key} ({spent_val} >= {cap_int})")

    return {
        "campaign_id": campaign_id,
        "spent": spent,
        "budget": budget,
        "remaining": remaining,
        "exhausted": exhausted,
        "reason": "; ".join(reasons) if reasons else "within_budget",
    }
