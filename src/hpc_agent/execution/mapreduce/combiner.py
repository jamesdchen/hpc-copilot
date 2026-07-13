#!/usr/bin/env python3
"""Standalone per-wave combiner deployed to the HPC cluster.

This script is scp'd to ``$REMOTE_PATH/.hpc/_hpc_combiner.py`` by
``deploy_runtime`` and executed on the login node after each wave
completes. It reads per-task ``metrics.json`` sidecars and aggregates
them into per-wave partial results grouped by grid point. Per-task
kwargs come from the run sidecar's frozen ``trial_params`` manifest
(the same ground truth the dispatcher uses); ``.hpc/tasks.py`` is
imported only for old sidecars that carry no manifest.

Stays zero-dependency — only Python stdlib, no imports from the
``hpc_agent`` package.

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
* Weighted means use ``n_samples`` as the weight (default 1 when absent;
  a partial whose entries carry no ``n_samples`` records its task count as
  ``_hpc_group_n`` so the cross-wave reduce stays task-weighted).

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

SUPPORTED_SCHEMA_VERSIONS = (1, 2)


def _default_max_workers():
    """Default thread-pool size: 2x CPU count, capped at 32 to spare NFS."""
    return min(32, (os.cpu_count() or 4) * 2)


def _read_metrics(metrics_path):
    with open(metrics_path, encoding="utf-8") as f:
        return json.load(f)


def _grid_key(params):
    """Deterministic string ID from kwargs values, joined by ``_``.

    Mirrors ``hpc_agent.execution.mapreduce.reduce.metrics.run_id`` semantics. Duplicated
    here because the combiner is deployed standalone (no package).

    Sort by key so two tasks with identical params but different dict
    insertion order group into the same grid point. The previous form
    ``"_".join(str(v) for v in params.values())`` was insertion-order
    sensitive — kwargs constructed in different orders produced
    different keys and silently split a single grid point.
    """
    raw = "_".join(str(params[k]) for k in sorted(params))
    return re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)


def _neumaier_sum(values):
    """Neumaier-compensated summation (improved Kahan).

    Reduces accumulated float rounding error. Handles the case where the
    running sum is smaller than the incoming term, which classic Kahan
    does not. Duplicated verbatim in ``hpc_agent/execution/mapreduce/reduce/metrics.py``.
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


def _coerce_weight(value, fallback):
    """Coerce an entry's ``n_samples`` to a usable non-negative weight.

    ``metrics.json`` is an arbitrary user JSON dict, so ``n_samples`` may
    be a string/list/negative value; ``v * w`` on a bad weight would raise
    and abort the whole wave. ``bool`` is excluded so ``True`` is not
    silently treated as the weight ``1``.
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)) and value >= 0:
        return value
    return fallback


def _weighted_mean(entries):
    if not entries:
        return {}

    all_keys = set()
    for e in entries:
        all_keys.update(e.keys())
    # Weight preference: the entry's own ``n_samples``, else the
    # ``_hpc_group_n`` group-size carrier a previous ``_weighted_mean`` pass
    # stamped on it (a per-wave partial), else 1 (a single task). Without the
    # carrier, a grid point split 9/1 across two waves weighted both partials
    # equally at the cross-wave reduce — the lone task got 9x its weight.
    weights = [_coerce_weight(e.get("n_samples", e.get("_hpc_group_n", 1)), 1) for e in entries]
    result = {}

    for key in sorted(all_keys):
        if key == "n_samples":
            result["n_samples"] = sum(_coerce_weight(e.get("n_samples", 0), 0) for e in entries)
            continue
        if key == "_hpc_group_n":
            # Weight carrier, never a metric — folded into the weights above
            # and re-emitted below while the group still has no n_samples.
            continue
        # Skip non-numeric values: write_metrics accepts an arbitrary
        # JSON dict, so a metrics.json may carry string/list labels;
        # ``v * w`` on those would raise and abort the whole wave.
        # ``weights`` is built one-to-one over ``entries`` above, so the two
        # are equal-length by construction — a bare ``zip`` is safe. The
        # ``strict=`` keyword is Python 3.10+, but this module is deployed
        # standalone and run under any cluster ``python3`` (>=3.8; RHEL/Rocky 8,
        # torch-1.x conda envs — see the deploy-floor lint and F18), where
        # ``zip(..., strict=True)`` raises ``TypeError`` and aborts every wave
        # combine. ``# noqa: B905`` stops a modernization pass from re-adding
        # ``strict=`` and re-raising the cluster floor.
        pairs = [
            (e[key], w)
            for e, w in zip(entries, weights)  # noqa: B905 - deploy floor <3.10; equal length by construction
            if key in e and isinstance(e[key], (int, float))
        ]
        if not pairs:
            continue
        w_total = _neumaier_sum(w for _, w in pairs)
        numerator = _neumaier_sum(v * w for v, w in pairs)
        result[key] = numerator / w_total if w_total else 0.0

    # Group-size carrier: when NO entry carried ``n_samples``, each weight
    # above is a group size (1 per raw task entry), so their sum is the task
    # count this aggregate covers. Stamped framework-namespaced (never
    # collides with a user metric) so the cross-wave reduce weights each
    # partial by its task count. Kept in sync with the copy in
    # ``hpc_agent/execution/mapreduce/reduce/metrics.py``.
    if "n_samples" not in result:
        result["_hpc_group_n"] = sum(weights)

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
        help="Overwrite an existing output (wave_N.json, or the final aggregate).",
    )
    parser.add_argument(
        "--final",
        action="store_true",
        help=(
            "Cross-wave FINAL reduce (#254): merge every _combiner/wave_*.json "
            "into a single _aggregated/<run_id>/metrics_aggregate.json so the "
            "local side pulls ONE file, not hundreds. Ignores --wave."
        ),
    )
    return parser.parse_args(argv)


def _wave_partial_files(out_dir):
    """``[(wave_num, path), ...]`` for ``_combiner/wave_<N>.json``, sorted by wave.

    Excludes the optional ``wave_<N>.runtime.json`` sidecars (the ``\\.json``
    anchor only matches the bare partial) so the final reduce sees exactly the
    per-wave aggregates the per-wave combiner wrote.
    """
    files = []
    if not os.path.isdir(out_dir):
        return files
    for name in os.listdir(out_dir):
        m = re.fullmatch(r"wave_(\d+)\.json", name)
        if m:
            files.append((int(m.group(1)), os.path.join(out_dir, name)))
    files.sort(key=lambda x: x[0])
    return files


def _final_reduce(*, run_id, force):
    """Cross-wave final reduce on the cluster (#254).

    Reads every ``_combiner/wave_<N>.json`` partial, merges their ``grid_points``
    across waves with the SAME weighted-mean (keyed on ``n_samples``) the local
    ``reduce_partials`` uses — so ``aggregated_metrics`` is byte-for-byte what
    the old pull-all-waves-then-reduce-locally path produced — and writes a
    single ``_aggregated/<run_id>/metrics_aggregate.json`` with a provenance
    footer (per-wave error counts, incomplete waves) and a manifest pointing
    back at the raw wave files (which stay on the cluster for drill-down).

    Runs in the remote project root (``run_combiner`` cd's there), so the
    ``_combiner`` / ``_aggregated`` paths are relative, exactly like the
    per-wave combiner's output. Stdlib-only — reuses this module's
    :func:`_weighted_mean`.
    """
    out_dir = "_combiner"
    agg_dir = os.path.join("_aggregated", run_id)
    agg_path = os.path.join(agg_dir, "metrics_aggregate.json")
    if os.path.exists(agg_path) and not force:
        print(
            f"[combiner] ERROR: final aggregate already exists: {agg_path} (use --force)",
            file=sys.stderr,
        )
        sys.exit(1)

    wave_files = _wave_partial_files(out_dir)
    if not wave_files:
        print(
            f"[combiner] ERROR: no {out_dir}/wave_*.json partials to reduce",
            file=sys.stderr,
        )
        sys.exit(1)

    partials = {}  # grid_key -> list[metric dicts] across waves
    waves_reduced = []
    errors_per_wave = {}
    incomplete_waves = []
    for wave_num, path in wave_files:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            errors_per_wave[str(wave_num)] = [f"unreadable: {exc}"]
            incomplete_waves.append(wave_num)
            continue
        waves_reduced.append(wave_num)
        wave_errors = data.get("errors") or []
        if wave_errors:
            errors_per_wave[str(wave_num)] = list(wave_errors)
            incomplete_waves.append(wave_num)
        for grid_key, metrics in (data.get("grid_points") or {}).items():
            partials.setdefault(grid_key, []).append(metrics)

    aggregated = {gk: _weighted_mean(partials[gk]) for gk in sorted(partials)}

    payload = {
        "run_id": run_id,
        "aggregated_metrics": aggregated,
        "waves": sorted(waves_reduced),
        "provenance": {
            "wave_count": len(waves_reduced),
            "incomplete_waves": sorted(set(incomplete_waves)),
            "errors_per_wave": errors_per_wave,
        },
        "manifest": {
            "wave_files": [f"{out_dir}/wave_{w}.json" for w, _ in wave_files],
        },
    }

    os.makedirs(agg_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="metrics_aggregate_", suffix=".json.tmp", dir=agg_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        os.replace(tmp, agg_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    print(f"[combiner] wrote {agg_path} (waves={len(waves_reduced)} grid_points={len(aggregated)})")


def main(max_workers=None, argv=None):
    here = Path(__file__).resolve().parent  # cluster-side .hpc/
    args = _parse_args(argv if argv is not None else [])

    # --- Final cross-wave reduce (#254) short-circuits the per-wave path ---
    # It needs only run_id + the _combiner/wave_*.json partials, not a --wave
    # or the sidecar/tasks.py, so branch before any of that is required.
    if args.final:
        run_id = args.run_id or os.environ.get("HPC_RUN_ID")
        if not run_id:
            print(
                "[combiner] ERROR: --final requires --run-id or HPC_RUN_ID",
                file=sys.stderr,
            )
            sys.exit(1)
        _final_reduce(run_id=run_id, force=args.force)
        return

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
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
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

    # Per-task summary filename the run declared (F-J). Absent → the historical
    # metrics.json literal, so an existing run is byte-identical. Resolved inline
    # (this module is deployed cluster-side standalone, no hpc_agent import) but
    # kept in lock-step with state.runs.resolved_summary_artifact.
    _declared_summary = sidecar.get("summary_artifact")
    summary_name = (
        _declared_summary.strip()
        if isinstance(_declared_summary, str) and _declared_summary.strip()
        else "metrics.json"
    )

    # --- Resolve the per-task kwargs source (frozen manifest first) ---
    # Mirrors dispatch.py's fast path: ``trial_params`` is serialized into the
    # sidecar at submit time and is the ground truth the tasks were hashed
    # (cmd_sha) and dispatched with. tasks.py must never be re-executed
    # cluster-side when the manifest exists — a state-dependent ``resolve()``
    # (the shipped optuna_strategy/pbt_strategy scaffolds) returns the NEXT
    # iteration's kwargs at combine time: wrong result_dirs, silently empty
    # partials, or a phantom optimizer trial minted on the login node.
    #
    # Fallback to importing tasks.py only when the sidecar has no frozen
    # manifest (old sidecars written before trial_params existed) — full
    # backward compatibility, same as the dispatcher.
    trial_params = sidecar.get("trial_params")
    if not isinstance(trial_params, list):
        trial_params = None
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
        if trial_params is not None:
            try:
                t = int(tid)
            except (TypeError, ValueError):
                errors.append(f"task {tid}: non-integer task id in wave_map")
                continue
            if not 0 <= t < len(trial_params):
                errors.append(
                    f"task {tid}: out of range of sidecar trial_params "
                    f"({len(trial_params)} entries)"
                )
                continue
            kwargs = trial_params[t]
            if not isinstance(kwargs, dict):
                errors.append(f"task {tid}: trial_params[{t}] is not a dict")
                continue
        else:
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
        metrics_path = os.path.join(result_dir, summary_name)
        if not os.path.isfile(metrics_path):
            errors.append(f"task {tid}: {summary_name} not found")
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
        from concurrent.futures import as_completed

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_info = {
                pool.submit(_read_metrics, metrics_path): (tid, grid_key, runtime_path)
                for tid, grid_key, metrics_path, runtime_path in readable
            }
            for future in as_completed(future_to_info):
                tid, grid_key, runtime_path = future_to_info[future]
                try:
                    metrics = future.result()
                except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    errors.append(f"task {tid}: failed to read {summary_name}: {exc}")
                    continue
                groups.setdefault(grid_key, []).append(metrics)
                # Best-effort runtime row. A malformed _runtime.json is
                # logged into errors but does NOT abort the wave —
                # warm-axis-picker contribution is optional, the
                # combiner's primary output (wave_<N>.json) is the
                # critical artifact.
                if runtime_path is not None:
                    try:
                        with open(runtime_path, encoding="utf-8") as rfh:
                            runtime_rows.append(json.load(rfh))
                    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
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
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(runtime_payload, f, indent=2, sort_keys=True)
                f.flush()
                with contextlib.suppress(OSError):
                    os.fsync(f.fileno())
            os.replace(tmp, runtime_out)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        print(f"[combiner] wrote {runtime_out} ({len(runtime_rows)} runtime samples)")


if __name__ == "__main__":
    main(argv=sys.argv[1:])
