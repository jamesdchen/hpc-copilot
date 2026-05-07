"""``campaign-budget`` primitive — track spent vs. supplied budget caps.

Pure read over sidecars tagged with *campaign_id*. Sums:

* ``jobs``         — number of completed run sidecars
* ``tasks``        — sum of ``task_count`` across completed runs
* ``walltime_sec`` — sum of per-task elapsed times if observable

Budget caps come in as CLI args; the framework holds no opinion about
defaults. Returns ``exhausted=True`` if any supplied cap is met.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


def _spent_walltime_sec(sidecar: dict[str, Any]) -> float:
    """Sum per-task elapsed_sec if last_status carries it; else 0."""
    last_status = sidecar.get("last_status") or {}
    tasks = last_status.get("tasks") or {}
    if not isinstance(tasks, dict):
        return 0.0
    total = 0.0
    for entry in tasks.values():
        if isinstance(entry, dict):
            elapsed = entry.get("elapsed_sec")
            if isinstance(elapsed, (int, float)):
                total += float(elapsed)
    return total


@primitive(
    name="campaign-budget",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-mapreduce campaign-budget",
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
    from claude_hpc.campaign.manifest import read_manifest
    from claude_hpc.mapreduce.reduce.history import find_sidecars_by_campaign

    manifest_budget: dict[str, Any] = {}
    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except Exception:
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
