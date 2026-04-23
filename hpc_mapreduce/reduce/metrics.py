"""Reduce per-task metric sidecars.

Standalone module — stdlib only, no external dependencies.
"""

from __future__ import annotations

__all__ = [
    "reduce_metrics",
    "reduce_by_grid_point",
    "reduce_backtest",
    "reduce_partials",
    "reduce_resource_usage",
]

import glob
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


def _neumaier_sum(values: Iterable[float]) -> float:
    """Neumaier-compensated summation (improved Kahan).

    Keeps reductions order-invariant within one ULP across task counts and
    dynamic ranges that would drift under plain ``sum``. Kept in sync with
    the copy in ``hpc_mapreduce/map/combiner.py``; the combiner runs
    standalone on the cluster and cannot import from this package.
    """
    s = 0.0
    c = 0.0
    for v in values:
        t = s + v
        if abs(s) >= abs(v):
            c += (s - t) + v
        else:
            c += (v - t) + s
        s = t
    return s + c


def reduce_metrics(result_dirs: Sequence[str | Path]) -> dict:
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
        w_total = _neumaier_sum(w for _, w in pairs)
        numerator = _neumaier_sum(v * w for v, w in pairs)
        result[key] = numerator / w_total if w_total else 0.0

    return result


def reduce_by_grid_point(manifest: dict) -> dict[str, dict]:
    """Group tasks by grid point, then reduce each group via :func:`reduce_metrics`.

    Tasks sharing the same ``params`` dict are treated as one grid point.

    Parameters
    ----------
    manifest : dict
        The task manifest (from :func:`build_task_manifest`).  Each task
        entry must have ``params`` and ``result_dir``.  Tasks are grouped
        by their ``params`` dict (via :func:`run_id`); any additional
        task-level keys are ignored.

    Returns
    -------
    dict mapping ``run_id`` (str) → aggregated metrics (dict).
    Grid points with no metrics files return empty dicts.
    """
    from hpc_mapreduce.job.grid import run_id as _run_id

    # Group tasks by grid point (via run_id over params)
    groups: dict[str, list[Path]] = {}
    for task in manifest["tasks"].values():
        key = _run_id(task["params"])
        groups.setdefault(key, []).append(Path(task["result_dir"]))

    results: dict[str, dict] = {}
    for grid_key, result_dirs in groups.items():
        results[grid_key] = reduce_metrics(result_dirs)

    return results


# Back-compat alias; remove in the next PR.
reduce_backtest = reduce_by_grid_point


def reduce_partials(combiner_dir: str | Path) -> dict[str, dict]:
    """Merge per-wave partial aggregates from _combiner/wave_*.json.

    Each wave file contains ``grid_points``: a mapping of run-id to
    aggregated metrics for that wave.  This function merges across
    waves using the same weighted-mean logic as :func:`reduce_metrics`,
    keyed on ``n_samples``.

    Parameters
    ----------
    combiner_dir : str or Path
        Directory containing ``wave_*.json`` files.

    Returns
    -------
    dict mapping run_id (str) to aggregated metrics (dict).
    """
    combiner_dir = Path(combiner_dir)
    wave_files = sorted(
        glob.glob(str(combiner_dir / "wave_*.json")),
        key=lambda p: int(Path(p).stem.split("_", 1)[1]),
    )

    # Collect partial entries per run_id across all waves
    partials: dict[str, list[dict]] = {}
    for wf in wave_files:
        try:
            with open(wf) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for run_id, metrics in data.get("grid_points", {}).items():
            partials.setdefault(run_id, []).append(metrics)

    # Weighted-mean aggregation per run_id (same logic as reduce_metrics)
    results: dict[str, dict] = {}
    for run_id, entries in partials.items():
        if not entries:
            results[run_id] = {}
            continue

        all_keys = {k for e in entries for k in e}
        weights = [e.get("n_samples", 1) for e in entries]
        agg: dict = {}

        for key in sorted(all_keys):
            if key == "n_samples":
                agg["n_samples"] = sum(e.get("n_samples", 0) for e in entries)
                continue
            pairs = [(e[key], w) for e, w in zip(entries, weights, strict=True) if key in e]
            if not pairs:
                continue
            w_total = _neumaier_sum(w for _, w in pairs)
            numerator = _neumaier_sum(v * w for v, w in pairs)
            agg[key] = numerator / w_total if w_total else 0.0

        results[run_id] = agg

    return results


def reduce_resource_usage(tasks: dict[str, dict] | dict[int, dict]) -> dict:
    """Sum per-task cpu_s / gpu_s / elapsed_s into a run-level cost rollup.

    Accepts the ``tasks`` map from a status report (string-keyed, 1-based task
    IDs) or the raw ``tasks`` dict from :func:`query_sacct` / :func:`query_sge`
    (int-keyed).  Missing keys are treated as 0 so partial/unknown tasks do
    not crash the rollup.

    Returns a dict with stable keys::

        {
            "cpu_hours": float,   # sum(cpu_s) / 3600
            "gpu_hours": float,   # sum(gpu_s) / 3600
            "elapsed_hours": float,  # sum(elapsed_s) / 3600 -- i.e. wall-time summed across tasks
            "tasks_counted": int, # number of tasks that contributed nonzero elapsed_s
        }
    """
    total_cpu_s = 0
    total_gpu_s = 0
    total_elapsed_s = 0
    counted = 0
    for info in (tasks or {}).values():
        if not isinstance(info, dict):
            continue
        elapsed = int(info.get("elapsed_s", 0) or 0)
        cpu = int(info.get("cpu_s", 0) or 0)
        gpu = int(info.get("gpu_s", 0) or 0)
        total_elapsed_s += elapsed
        total_cpu_s += cpu
        total_gpu_s += gpu
        if elapsed > 0:
            counted += 1
    return {
        "cpu_hours": round(total_cpu_s / 3600.0, 4),
        "gpu_hours": round(total_gpu_s / 3600.0, 4),
        "elapsed_hours": round(total_elapsed_s / 3600.0, 4),
        "tasks_counted": counted,
    }
