"""Reduce per-task metric sidecars.

Standalone module — stdlib only, no external dependencies.
"""

from __future__ import annotations

__all__ = [
    "reduce_metrics",
    "reduce_backtest",
]

import json
from pathlib import Path


def reduce_metrics(result_dirs: list[str | Path]) -> dict:
    """Reduce per-task metrics JSON sidecars into a single summary.

    Computes a weighted mean of each metric key across tasks, weighted by
    ``n_samples`` when present.  The ``n_samples`` key itself is summed.
    Missing or corrupt sidecar files are silently skipped.

    Parameters
    ----------
    result_dirs : list of str or Path
        Directories to scan for a ``metrics.json`` file in each.

    Returns
    -------
    dict
        Flat dict of aggregated metrics.  Empty dict if no sidecars found.
    """
    entries: list[dict] = []

    for rdir in result_dirs:
        path = Path(rdir) / "metrics.json"
        if not path.exists():
            continue
        try:
            with open(path) as f:
                entries.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue

    if not entries:
        return {}

    all_keys = {k for e in entries for k in e}
    weights = [e.get("n_samples", 1) for e in entries]
    result: dict = {}

    for key in sorted(all_keys):
        if key == "n_samples":
            result["n_samples"] = sum(e.get("n_samples", 0) for e in entries)
            continue
        pairs = [(e[key], w) for e, w in zip(entries, weights, strict=True) if key in e]
        if not pairs:
            continue
        w_total = sum(w for _, w in pairs)
        result[key] = sum(v * w for v, w in pairs) / w_total if w_total else 0.0

    return result


def reduce_backtest(manifest: dict) -> dict[str, dict]:
    """Reduce metrics along the backtest time-period axis.

    Groups tasks by grid point (same ``params``), computes per-period
    metrics from each task's ``metrics.json`` sidecar, then averages
    across periods per grid point (weighted by ``n_samples`` when present).

    Parameters
    ----------
    manifest : dict
        The task manifest (from :func:`build_task_manifest`).  Each task
        entry must have ``params`` and ``result_dir``.  Tasks with a
        ``period`` key are grouped; tasks without periods are treated
        as single-period grid points.

    Returns
    -------
    dict mapping ``run_id`` (str) → aggregated metrics (dict).
    Grid points with no metrics files return empty dicts.
    """
    from hpc_mapreduce.job.grid import run_id as _run_id

    # Group tasks by grid point (params without period)
    groups: dict[str, list[Path]] = {}
    for task in manifest["tasks"].values():
        key = _run_id(task["params"])
        groups.setdefault(key, []).append(Path(task["result_dir"]))

    results: dict[str, dict] = {}
    for grid_key, result_dirs in groups.items():
        results[grid_key] = reduce_metrics(result_dirs)

    return results
