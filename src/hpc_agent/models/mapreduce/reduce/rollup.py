"""Per-task status report rollup helpers.

Extracted from :mod:`status` for navigability — that module was 752
LOC mixing per-task status checking, scheduler dispatch, and these
post-processing rollups. The two ``rollup_by_*`` helpers run after
:func:`status.report_status_from_tasks` produces the per-task report;
they fold the report by grid point or by wave for downstream consumers
(``hpc-agent status`` envelope, the campaign loop's ``prior(...)``
view, the rich TUI).
"""

from __future__ import annotations

__all__ = [
    "rollup_by_grid_point",
    "rollup_by_wave",
]

# Status keys we count into per-status buckets. The bucket dict ALSO
# carries a sibling ``"total"`` key; using ``status in bucket`` to gate
# the increment would treat a literal status value of ``"total"`` (from
# a corrupt / legacy report) as a valid bucket and double-increment the
# total counter.
_STATUS_BUCKETS = frozenset({"complete", "running", "pending", "failed", "unknown"})


def _grid_point_key(params: dict) -> str:
    """Stable grid-point identifier from a params dict."""
    if not params:
        return "_"
    return "_".join(f"{k}={params[k]}" for k in sorted(params))


def rollup_by_grid_point(report: dict, tasks_data: dict) -> dict[str, dict]:
    """Group per-task statuses in *report* by grid point (from task ``params``).

    Per-task dict task IDs are 0-based strings; report task IDs are 1-based strings.
    Returned dict maps grid-point key -> ``{complete, running, pending, failed, unknown, total}``.
    """
    rollup: dict[str, dict] = {}
    task_entries = tasks_data.get("tasks", {})
    for tid_str, task_info in report.get("tasks", {}).items():
        try:
            entry_key = str(int(tid_str) - 1)
        except (TypeError, ValueError):
            continue
        entry = task_entries.get(entry_key)
        if entry is None:
            continue
        gp = _grid_point_key(entry.get("params") or {})
        bucket = rollup.setdefault(
            gp,
            {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0, "total": 0},
        )
        bucket["total"] += 1
        status = task_info.get("status", "unknown")
        if status in _STATUS_BUCKETS:
            bucket[status] += 1
        else:
            bucket["unknown"] += 1
    return rollup


def rollup_by_wave(report: dict, tasks_data: dict) -> dict[str, dict]:
    """Group per-task statuses by wave (from task ``wave_map``).

    Returns ``{wave: {complete, running, pending, failed, unknown, total}}``.
    Empty when the per-task dict has no ``wave_map`` (un-batched submissions).

    Wave map keys are stored as 0-based task ids; the
    status report keys tasks 1-based to match scheduler array indexing,
    so we shift on lookup.
    """
    wave_map = tasks_data.get("wave_map") or {}
    if not wave_map:
        return {}
    report_tasks = report.get("tasks", {}) or {}
    rollup: dict[str, dict] = {}
    for wave_key, members in wave_map.items():
        bucket = {
            "complete": 0,
            "running": 0,
            "pending": 0,
            "failed": 0,
            "unknown": 0,
            "total": 0,
        }
        for tid in members or []:
            bucket["total"] += 1
            # Per-task dict stores 0-based; report keys 1-based.
            try:
                report_key = str(int(tid) + 1)
            except (TypeError, ValueError):
                report_key = str(tid)
            task_info = report_tasks.get(report_key) or {}
            status = task_info.get("status", "unknown")
            if status in _STATUS_BUCKETS:
                bucket[status] += 1
            else:
                bucket["unknown"] += 1
        rollup[str(wave_key)] = bucket
    return rollup
