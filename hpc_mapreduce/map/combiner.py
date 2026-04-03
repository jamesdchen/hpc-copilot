#!/usr/bin/env python3
"""Standalone per-wave combiner deployed to the HPC cluster.

This script is rsynced to the cluster and executed on the login node after
each wave completes.  It reads per-task ``metrics.json`` sidecars and
aggregates them into per-wave partial results grouped by grid point.
It must remain zero-dependency — only Python stdlib, no imports from the
``hpc_mapreduce`` package.
"""

import json
import os
import re
import sys

__all__ = ["main"]


# Duplicated from hpc_mapreduce.job.grid.run_id — this script cannot import
# from the package because it runs standalone on the cluster.
def _run_id(params: dict) -> str:
    """Deterministic string ID from param values, joined by ``_``."""
    raw = "_".join(params.values())
    return re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)


def _weighted_mean(entries: list, errors: list) -> dict:
    """Compute weighted-mean metrics across *entries*.

    Mirrors the algorithm in ``hpc_mapreduce.reduce.metrics.reduce_metrics``:
    every metric key is averaged weighted by ``n_samples`` (defaulting to 1),
    while ``n_samples`` itself is summed.
    """
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


def main() -> None:
    # --- Read env vars ---
    wave_str = os.environ.get("HPC_WAVE")
    if wave_str is None:
        print("[combiner] ERROR: HPC_WAVE env var not set", file=sys.stderr)
        sys.exit(1)

    try:
        wave = int(wave_str)
    except ValueError:
        print(f"[combiner] ERROR: HPC_WAVE is not an integer: {wave_str!r}", file=sys.stderr)
        sys.exit(1)

    manifest_path = os.environ.get("HPC_MANIFEST", "_hpc_dispatch.json")

    # --- Load manifest ---
    if not os.path.isfile(manifest_path):
        print(f"[combiner] ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"[combiner] ERROR: failed to parse manifest: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Resolve wave ---
    wave_map = manifest.get("wave_map", {})
    wave_key = str(wave)
    if wave_key not in wave_map:
        print(
            f"[combiner] ERROR: wave {wave} not found in wave_map "
            f"(available: {sorted(wave_map.keys())})",
            file=sys.stderr,
        )
        sys.exit(1)

    task_ids = wave_map[wave_key]
    tasks = manifest.get("tasks", {})

    print(f"[combiner] wave={wave} tasks={len(task_ids)}")

    # --- Read metrics per task ---
    errors: list = []
    # grid_point -> list of metric dicts
    groups: dict = {}

    for tid in task_ids:
        task = tasks.get(str(tid))
        if task is None:
            errors.append(f"task {tid}: not found in manifest")
            continue

        result_dir = task["result_dir"]
        metrics_path = os.path.join(result_dir, "metrics.json")

        if not os.path.isfile(metrics_path):
            errors.append(f"task {tid}: metrics.json not found")
            continue

        try:
            with open(metrics_path) as f:
                metrics = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"task {tid}: failed to read metrics.json: {exc}")
            continue

        grid_key = _run_id(task.get("params", {}))
        groups.setdefault(grid_key, []).append(metrics)

    # --- Aggregate per grid point ---
    grid_points: dict = {}
    for grid_key in sorted(groups):
        grid_points[grid_key] = _weighted_mean(groups[grid_key], errors)

    print(f"[combiner] grid_points={len(grid_points)} errors={len(errors)}")

    # --- Write output ---
    out_dir = "_combiner"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"wave_{wave}.json")

    output = {
        "wave": wave,
        "task_ids": list(task_ids),
        "grid_points": grid_points,
        "errors": errors,
    }

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"[combiner] wrote {out_path}")


if __name__ == "__main__":
    main()
