#!/usr/bin/env python3
"""Standalone per-wave combiner deployed to the HPC cluster.

This script is scp'd to ``$REMOTE_PATH/.hpc/_hpc_combiner.py`` by
``deploy_runtime`` and executed on the login node after each wave
completes. It reads per-task ``metrics.json`` sidecars and aggregates
them into per-wave partial results grouped by grid point.

Stays zero-dependency — only Python stdlib, no imports from the
``claude_hpc`` package.

CLI:

    python3 _hpc_combiner.py --wave 0 --run-id <id> [--force]

Environment variable fallbacks (``HPC_WAVE``, ``HPC_RUN_ID``) remain
supported for already-deployed copies on clusters that invoke the
combiner via env.

Exit codes:
    0  - success
    1  - bad input (missing wave, malformed sidecar, output already exists)

Determinism guarantee
---------------------
The combiner produces bit-identical output for a given input set,
independent of file enumeration order or wave-completion timing:

* Per-task sidecars are grouped by grid point, then keys within each
  group are iterated via ``sorted()`` (see ``for grid_key in sorted(...)``
  and ``for key in sorted(all_keys)``).
* Reductions use Neumaier-compensated summation (Kahan with high-magnitude
  swap), which is order-invariant for floats up to the compensation term's
  precision — re-running the combiner on the same per-task outputs produces
  the same aggregate regardless of which task finished first.
* Weighted means use ``n_samples`` as the weight (default 1 when absent).

This means: if your executors are deterministic for a given task_id (see
the executor scaffold for the seed-from-HPC_TASK_ID pattern), the entire
local-side aggregate is reproducible across re-runs of the combiner.
The framework does not introduce nondeterminism into the reduction.
"""

import argparse
import contextlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

__all__ = ["main"]

SUPPORTED_SCHEMA_VERSIONS = (1,)


def _default_max_workers():
    """Default thread-pool size: 2x CPU count, capped at 32 to spare NFS."""
    return min(32, (os.cpu_count() or 4) * 2)


def _read_metrics(metrics_path):
    with open(metrics_path) as f:
        return json.load(f)


def _grid_key(params):
    """Deterministic string ID from kwargs values, joined by ``_``.

    Mirrors ``claude_hpc.mapreduce.reduce.metrics.run_id`` semantics. Duplicated
    here because the combiner is deployed standalone (no package).
    """
    raw = "_".join(str(v) for v in params.values())
    return re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)


def _neumaier_sum(values):
    """Neumaier-compensated summation (improved Kahan).

    Reduces accumulated float rounding error. Handles the case where the
    running sum is smaller than the incoming term, which classic Kahan
    does not. Duplicated verbatim in ``claude_hpc/mapreduce/reduce/metrics.py``.
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


def _weighted_mean(entries):
    if not entries:
        return {}

    all_keys = set()
    for e in entries:
        all_keys.update(e.keys())
    weights = [e.get("n_samples", 1) for e in entries]
    result = {}

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


def _load_tasks_module(tasks_py_path):
    spec = importlib.util.spec_from_file_location("hpc_user_tasks", tasks_py_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load tasks.py from {tasks_py_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _format_result_dir(template, *, task_id, run_id, kwargs):
    ctx = {"task_id": task_id, "run_id": run_id, **kwargs}
    return template.format(**ctx)


def _parse_args(argv):
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
        "--run-id",
        default=None,
        help="Run ID — locates the sidecar at .hpc/runs/<run_id>.json. Falls back to $HPC_RUN_ID.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing _combiner/wave_N.json output.",
    )
    return parser.parse_args(argv)


def main(max_workers=None, argv=None):
    here = Path(__file__).resolve().parent  # cluster-side .hpc/
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

    # --- Resolve run_id ---
    run_id = args.run_id or os.environ.get("HPC_RUN_ID")
    if not run_id:
        print(
            "[combiner] ERROR: --run-id not given and HPC_RUN_ID env var not set",
            file=sys.stderr,
        )
        sys.exit(1)

    sidecar_path = here / "runs" / f"{run_id}.json"
    if not sidecar_path.is_file():
        print(f"[combiner] ERROR: sidecar not found: {sidecar_path}", file=sys.stderr)
        sys.exit(1)

    try:
        sidecar = json.loads(sidecar_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"[combiner] ERROR: failed to parse sidecar: {exc}", file=sys.stderr)
        sys.exit(1)

    schema_version = sidecar.get("sidecar_schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        print(
            f"[combiner] ERROR: sidecar schema_version={schema_version}, "
            f"supported={list(SUPPORTED_SCHEMA_VERSIONS)}.",
            file=sys.stderr,
        )
        sys.exit(1)

    wave_map = sidecar.get("wave_map", {})
    wave_key = str(wave)
    if wave_key not in wave_map:
        print(
            f"[combiner] ERROR: wave {wave} not in wave_map (available: {sorted(wave_map.keys())})",
            file=sys.stderr,
        )
        sys.exit(1)

    task_ids = wave_map[wave_key]
    result_dir_template = sidecar.get("result_dir_template")
    if not result_dir_template:
        print("[combiner] ERROR: sidecar missing result_dir_template", file=sys.stderr)
        sys.exit(1)

    # --- Load tasks.py ---
    tasks_path = here / "tasks.py"
    if not tasks_path.is_file():
        print(f"[combiner] ERROR: tasks.py not found: {tasks_path}", file=sys.stderr)
        sys.exit(1)
    try:
        tasks = _load_tasks_module(tasks_path)
    except Exception as exc:
        print(f"[combiner] ERROR: failed to import tasks.py: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Output path & --force handling (check BEFORE doing any work) ---
    out_dir = "_combiner"
    out_path = os.path.join(out_dir, f"wave_{wave}.json")
    if os.path.exists(out_path) and not args.force:
        print(
            f"[combiner] ERROR: output already exists: {out_path} (use --force to overwrite)",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[combiner] wave={wave} run_id={run_id} tasks={len(task_ids)}")

    # --- Read metrics per task (parallelized — I/O-bound over NFS) ---
    errors = []
    groups = {}  # grid_key -> list of metric dicts
    readable = []  # (tid, grid_key, metrics_path, runtime_path)

    for tid in task_ids:
        try:
            kwargs = tasks.resolve(int(tid))
        except Exception as exc:
            errors.append(f"task {tid}: tasks.resolve raised: {exc}")
            continue
        if not isinstance(kwargs, dict):
            errors.append(f"task {tid}: tasks.resolve returned non-dict")
            continue
        try:
            result_dir = _format_result_dir(
                result_dir_template, task_id=int(tid), run_id=run_id, kwargs=kwargs
            )
        except KeyError as exc:
            errors.append(f"task {tid}: result_dir_template missing key {exc.args[0]!r}")
            continue
        metrics_path = os.path.join(result_dir, "metrics.json")
        if not os.path.isfile(metrics_path):
            errors.append(f"task {tid}: metrics.json not found")
            continue
        # Per-task runtime sidecar (timing + axis_bindings) is optional —
        # the dispatcher writes it best-effort. Missing → no warm-picker
        # contribution for this task; the rest of the pipeline still
        # works fine.
        runtime_path = os.path.join(result_dir, "_runtime.json")
        runtime_path = runtime_path if os.path.isfile(runtime_path) else None
        readable.append((tid, _grid_key(kwargs), metrics_path, runtime_path))

    workers = max_workers if max_workers is not None else _default_max_workers()
    workers = max(1, min(workers, len(readable))) if readable else 1

    runtime_rows = []  # one dict per task with a readable _runtime.json
    if readable:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_info = {
                pool.submit(_read_metrics, metrics_path): (tid, grid_key, runtime_path)
                for tid, grid_key, metrics_path, runtime_path in readable
            }
            for future in future_to_info:
                tid, grid_key, runtime_path = future_to_info[future]
                try:
                    metrics = future.result()
                except (json.JSONDecodeError, OSError) as exc:
                    errors.append(f"task {tid}: failed to read metrics.json: {exc}")
                    continue
                groups.setdefault(grid_key, []).append(metrics)
                # Best-effort runtime row. A malformed _runtime.json is
                # logged into errors but does NOT abort the wave —
                # warm-axis-picker contribution is optional, the
                # combiner's primary output (wave_<N>.json) is the
                # critical artifact.
                if runtime_path is not None:
                    try:
                        with open(runtime_path) as rfh:
                            runtime_rows.append(json.load(rfh))
                    except (json.JSONDecodeError, OSError) as exc:
                        errors.append(f"task {tid}: failed to read _runtime.json: {exc}")

    # --- Aggregate per grid point ---
    grid_points = {}
    for grid_key in sorted(groups):
        grid_points[grid_key] = _weighted_mean(groups[grid_key])

    print(f"[combiner] grid_points={len(grid_points)} errors={len(errors)}")

    # --- Write output ---
    os.makedirs(out_dir, exist_ok=True)

    output = {
        "wave": wave,
        "run_id": run_id,
        "task_ids": list(task_ids),
        "grid_points": grid_points,
        "errors": errors,
    }

    # Atomic write: tempfile + os.replace. Critical because callers treat
    # ``wave_<N>.json`` *existence* as the "wave combined" success marker.
    fd, tmp = tempfile.mkstemp(prefix="wave_", suffix=".json.tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(output, f, indent=2)
        os.replace(tmp, out_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

    print(f"[combiner] wrote {out_path}")

    # Runtime sidecar — feeds the warm-axis-picker on the local side via
    # ``aggregate_flow``'s ingest step. Skip emission entirely when no
    # rows survived (e.g. dispatcher couldn't write _runtime.json — most
    # likely permission issue) so the local rsync_pull doesn't pick up
    # an empty file. Atomic write same as wave_<N>.json.
    if runtime_rows:
        runtime_out = os.path.join(out_dir, f"wave_{wave}.runtime.json")
        runtime_payload = {
            "wave": wave,
            "run_id": run_id,
            "samples": runtime_rows,
        }
        fd, tmp = tempfile.mkstemp(prefix="wave_", suffix=".runtime.json.tmp", dir=out_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(runtime_payload, f, indent=2, sort_keys=True)
            os.replace(tmp, runtime_out)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        print(f"[combiner] wrote {runtime_out} ({len(runtime_rows)} runtime samples)")


if __name__ == "__main__":
    main(argv=sys.argv[1:])
