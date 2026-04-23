#!/usr/bin/env python3
"""Standalone per-wave combiner deployed to the HPC cluster.

This script is rsynced to the cluster and executed on the login node after
each wave completes.  It reads per-task ``metrics.json`` sidecars and
aggregates them into per-wave partial results grouped by grid point.
It must remain zero-dependency -- only Python stdlib, no imports from the
``hpc_mapreduce`` package.

CLI (preferred):

    python3 _hpc_combiner.py --wave 0 --manifest _hpc_dispatch.json [--force]

Environment variables (``HPC_WAVE`` / ``HPC_MANIFEST``) remain the fallback
for already-deployed copies on clusters that invoke the combiner via env.

Exit codes:
    0  - success
    1  - bad input (missing wave, malformed manifest, output already exists)
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

__all__ = ["main"]


def _default_max_workers() -> int:
    """Default thread-pool size: 2x CPU count, capped at 32 to spare NFS."""
    return min(32, (os.cpu_count() or 4) * 2)


def _read_metrics(metrics_path):
    """Read and parse one ``metrics.json`` file.

    Raises ``FileNotFoundError`` if missing, ``json.JSONDecodeError`` /
    ``OSError`` on read/parse failure -- the caller translates these into
    per-task error strings.
    """
    with open(metrics_path) as f:
        data = json.load(f)
    return data


# Duplicated from hpc_mapreduce.job.grid.run_id -- this script cannot import
# from the package because it runs standalone on the cluster.
def _run_id(params):
    """Deterministic string ID from param values, joined by ``_``."""
    raw = "_".join(params.values())
    return re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)


def _neumaier_sum(values):
    """Neumaier-compensated summation (improved Kahan).

    Reduces accumulated float rounding error over long or wide-dynamic-range
    sequences, so cross-task aggregates in the combiner are order-invariant
    within one ULP. Handles the case where the running sum is smaller than
    the incoming term -- which classic Kahan does not.

    Duplicated verbatim in ``hpc_mapreduce/reduce/metrics.py``; keep them in
    sync. Not imported from there because this module is deployed standalone
    to the cluster (no package available).
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


def _weighted_mean(entries, errors):
    """Compute weighted-mean metrics across *entries*.

    Mirrors the algorithm in ``hpc_mapreduce.reduce.metrics.reduce_metrics``:
    every metric key is averaged weighted by ``n_samples`` (defaulting to 1),
    while ``n_samples`` itself is summed.  Uses Neumaier-compensated
    summation for numerator and denominator so the aggregate is stable
    regardless of task arrival order.
    """
    if not entries:
        return {}

    all_keys = set()
    for e in entries:
        all_keys.update(e.keys())
    weights = [e.get("n_samples", 1) for e in entries]
    result = {}

    for key in sorted(all_keys):
        if key == "n_samples":
            # n_samples is an integer count -- plain sum is exact.
            result["n_samples"] = sum(e.get("n_samples", 0) for e in entries)
            continue
        pairs = [(e[key], w) for e, w in zip(entries, weights, strict=True) if key in e]
        if not pairs:
            continue
        w_total = _neumaier_sum(w for _, w in pairs)
        numerator = _neumaier_sum(v * w for v, w in pairs)
        result[key] = numerator / w_total if w_total else 0.0

    return result


def _parse_args(argv):
    """Parse CLI args.  Falls back to env vars for unset flags (back-compat)."""
    parser = argparse.ArgumentParser(
        description="Per-wave combiner: aggregate metrics.json into wave_N.json.",
    )
    parser.add_argument(
        "--wave",
        type=int,
        default=None,
        help="Wave number (0-based). Falls back to $HPC_WAVE.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to the dispatch manifest. Falls back to $HPC_MANIFEST or '_hpc_dispatch.json'.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing _combiner/wave_N.json output.",
    )
    return parser.parse_args(argv)


def main(max_workers=None, argv=None):
    # When invoked from __main__ the entry block passes sys.argv[1:] explicitly;
    # when called programmatically (e.g. tests) with argv=None we default to []
    # so argparse doesn't inherit the host process's argv.
    args = _parse_args(argv if argv is not None else [])

    # --- Resolve wave: CLI first, env var fallback ---
    if args.wave is not None:
        wave = args.wave
    else:
        wave_str = os.environ.get("HPC_WAVE")
        if wave_str is None:
            print(
                "[combiner] ERROR: --wave not given and HPC_WAVE env var not set",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            wave = int(wave_str)
        except ValueError:
            print(
                f"[combiner] ERROR: HPC_WAVE is not an integer: {wave_str!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    # --- Resolve manifest path: CLI first, then env, then default ---
    if args.manifest is not None:
        manifest_path = args.manifest
    else:
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

    # --- Output path & --force handling (check BEFORE doing any work) ---
    out_dir = "_combiner"
    out_path = os.path.join(out_dir, f"wave_{wave}.json")
    if os.path.exists(out_path) and not args.force:
        print(
            f"[combiner] ERROR: output already exists: {out_path} (use --force to overwrite)",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[combiner] wave={wave} tasks={len(task_ids)}")

    # --- Read metrics per task (parallelized -- I/O-bound over NFS) ---
    errors = []
    # grid_point -> list of metric dicts
    groups = {}

    # Pre-resolve tasks into (tid, task, metrics_path) triples.  Tasks with
    # no manifest entry or no metrics.json are short-circuited into errors
    # here so the thread pool only handles genuine file reads.
    readable = []  # list of (tid, grid_key, metrics_path)
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

        grid_key = _run_id(task.get("params", {}))
        readable.append((tid, grid_key, metrics_path))

    workers = max_workers if max_workers is not None else _default_max_workers()
    # Don't spin up more threads than there is work to do.
    workers = max(1, min(workers, len(readable))) if readable else 1

    if readable:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Submit in task_id order; map future -> (tid, grid_key) so we
            # can still attribute errors to the right task.
            future_to_info = {
                pool.submit(_read_metrics, metrics_path): (tid, grid_key)
                for tid, grid_key, metrics_path in readable
            }
            # Iterate in submission order so error messages / appended
            # metrics are in a deterministic order (task_id order).  The
            # weighted-mean aggregate is order-independent, but deterministic
            # order makes logs easier to read.
            for future in future_to_info:
                tid, grid_key = future_to_info[future]
                try:
                    metrics = future.result()
                except (json.JSONDecodeError, OSError) as exc:
                    errors.append(f"task {tid}: failed to read metrics.json: {exc}")
                    continue
                groups.setdefault(grid_key, []).append(metrics)

    # --- Aggregate per grid point ---
    grid_points = {}
    for grid_key in sorted(groups):
        grid_points[grid_key] = _weighted_mean(groups[grid_key], errors)

    print(f"[combiner] grid_points={len(grid_points)} errors={len(errors)}")

    # --- Write output ---
    os.makedirs(out_dir, exist_ok=True)

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
    main(argv=sys.argv[1:])
