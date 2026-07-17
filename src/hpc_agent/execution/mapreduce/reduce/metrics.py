"""Reduce per-task metric sidecars.

Standalone module — stdlib only, no external dependencies.
"""

from __future__ import annotations

__all__ = [
    "reduce_metrics",
    "reduce_by_grid_point",
    "reduce_partials",
    "reduce_resource_usage",
]

import glob
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# Combiner partial files are ``wave_<N>.json``. The combiner also writes
# ``wave_<N>.runtime.json`` siblings into the same dir, which a bare
# ``wave_*.json`` glob matches — and ``int("3.runtime")`` then crashes
# the wave-number sort. Match strictly so runtime files are excluded.
_WAVE_FILE_RE = re.compile(r"^wave_\d+\.json$")


def _wave_partial_files(combiner_dir: Path, run_id: str | None = None) -> list[str]:
    """``wave_<N>.json`` partial files under *combiner_dir* (runtime files excluded).

    Run-scoped layout (BR-9 / DB1): the current cluster combiner writes each
    partial under a RUN-SCOPED subdir ``_combiner/<run_id>/wave_<N>.json`` so two
    runs sharing a ``remote_path`` can never clobber each other's partials by
    construction. An OLDER deployed combiner still writes ``_combiner/wave_<N>.json``
    directly, and the ``_combiner/`` pull brings BOTH layouts down, so this reader
    accepts both: it prefers the run-scoped copy for any wave present in both (a
    re-scoped force-recombine must never be double-counted with its legacy-flat
    twin), falling back to the legacy-flat copy otherwise. The legacy-flat copies
    stay subject to the F05 foreign-run filter in the caller (a foreign run can only
    collide on the flat layout); a run-scoped copy is inherently this run's own.
    With *run_id* ``None`` the run-scoped subdir cannot be named, so only the
    legacy-flat layout is scanned (the historical behavior, and every fixture that
    writes partials flat)."""
    by_wave: dict[int, str] = {}
    # Legacy-flat directly under combiner_dir (lower precedence).
    for p in glob.glob(str(combiner_dir / "wave_*.json")):
        name = Path(p).name
        if _WAVE_FILE_RE.match(name):
            by_wave[int(name[len("wave_") : -len(".json")])] = p
    # Run-scoped ``combiner_dir/<run_id>/wave_*.json`` overrides the flat twin.
    if run_id:
        for p in glob.glob(str(combiner_dir / run_id / "wave_*.json")):
            name = Path(p).name
            if _WAVE_FILE_RE.match(name):
                by_wave[int(name[len("wave_") : -len(".json")])] = p
    return [by_wave[w] for w in sorted(by_wave)]


def _neumaier_sum(values: Iterable[float]) -> float:
    """Neumaier-compensated summation (improved Kahan).

    Keeps reductions order-invariant within one ULP across task counts and
    dynamic ranges that would drift under plain ``sum``. Kept in sync with
    the copy in ``hpc_agent/execution/mapreduce/combiner.py``; the combiner runs
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


def _coerce_weight(value, fallback):
    """Coerce an entry's ``n_samples`` to a usable non-negative weight.

    A ``metrics.json`` is an arbitrary user JSON dict, so ``n_samples``
    may be a string/list/negative value; ``v * w`` on a bad weight would
    raise and abort the whole reduce. ``bool`` is excluded so ``True`` is
    not silently treated as the weight ``1``. Kept in sync with the copy
    in ``hpc_agent/execution/mapreduce/combiner.py``.
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)) and value >= 0:
        return value
    return fallback


def _weighted_mean(entries: list[dict]) -> dict:
    """Per-key weighted-mean rollup across a list of metric dicts.

    Used by both :func:`reduce_metrics` (per-task sidecars) and
    :func:`reduce_partials` (per-wave grid_points entries). Each entry
    contributes its keys weighted by ``n_samples`` (default 1 per entry
    when missing); the resulting ``n_samples`` is the plain sum. When no
    entry carries ``n_samples``, the output instead carries the
    framework-namespaced ``_hpc_group_n`` group-size carrier (the task
    count), so a later pass over these partials weights each by its
    group size rather than equally.

    Empty input returns an empty dict.
    """
    if not entries:
        return {}

    all_keys = {k for e in entries for k in e}
    # Weight preference: the entry's own ``n_samples``, else the
    # ``_hpc_group_n`` group-size carrier a previous ``_weighted_mean`` pass
    # stamped on it (a per-wave partial), else 1 (a single task). Without the
    # carrier, a grid point split 9/1 across two waves weighted both partials
    # equally at the cross-wave reduce — the lone task got 9x its weight.
    weights = [_coerce_weight(e.get("n_samples", e.get("_hpc_group_n", 1)), 1) for e in entries]
    agg: dict = {}

    for key in sorted(all_keys):
        if key == "n_samples":
            agg["n_samples"] = sum(_coerce_weight(e.get("n_samples", 0), 0) for e in entries)
            continue
        if key == "_hpc_group_n":
            # Weight carrier, never a metric — folded into the weights above
            # and re-emitted below while the group still has no n_samples.
            continue
        # Skip non-numeric values: a metrics.json may carry string/list
        # labels, and ``v * w`` on those would raise. Kept in sync with
        # the combiner's copy of this helper.
        pairs = [
            (e[key], w)
            for e, w in zip(entries, weights, strict=True)
            if key in e and isinstance(e[key], (int, float))
        ]
        if not pairs:
            continue
        w_total = _neumaier_sum(w for _, w in pairs)
        numerator = _neumaier_sum(v * w for v, w in pairs)
        agg[key] = numerator / w_total if w_total else 0.0

    # Group-size carrier: when NO entry carried ``n_samples``, each weight
    # above is a group size (1 per raw task entry), so their sum is the task
    # count this aggregate covers. Stamped framework-namespaced (never
    # collides with a user metric) so the cross-wave reduce weights each
    # partial by its task count. Kept in sync with the copy in
    # ``hpc_agent/execution/mapreduce/combiner.py``.
    if "n_samples" not in agg:
        agg["_hpc_group_n"] = sum(weights)

    return agg


def reduce_metrics(
    result_dirs: Sequence[str | Path],
    *,
    filename: str = "metrics.json",
) -> dict:
    """Reduce per-task summary JSON sidecars into a single summary.

    Computes a weighted mean of each metric key across tasks, weighted by
    ``n_samples`` when present.  The ``n_samples`` key itself is summed.
    Missing or corrupt sidecar files are silently skipped.

    Parameters
    ----------
    result_dirs : list of str or Path
        Directories to scan for the per-task summary file in each.
    filename : str
        The per-task summary filename to read in each dir. Defaults to
        ``metrics.json`` (the historical hardcode); callers thread the run's
        declared ``summary_artifact`` (F-J) so a non-default emitter is read.

    Returns
    -------
    dict
        Flat dict of aggregated metrics.  Empty dict if no sidecars found.
    """
    entries: list[dict] = []

    for rdir in result_dirs:
        path = Path(rdir) / filename
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                entries.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue

    return _weighted_mean(entries)


def reduce_by_grid_point(tasks_data: dict) -> dict[str, dict]:
    """Group tasks by grid point, then reduce each group via :func:`reduce_metrics`.

    Tasks sharing the same ``params`` dict are treated as one grid point.

    Parameters
    ----------
    tasks_data : dict
        Per-task dict with ``tasks.<tid>.params`` and
        ``tasks.<tid>.result_dir`` fields. Typically the synthetic dict
        produced from a per-run sidecar + ``.hpc/tasks.py`` by
        :func:`hpc_agent.execution.mapreduce.reduce.status._build_per_task_dict_from_sidecar`.
        Tasks are grouped by their ``params`` dict (via the inlined
        ``run_id`` helper); any additional task-level keys are ignored.

    Returns
    -------
    dict mapping grid-point key (str) → aggregated metrics (dict).
    Grid points with no metrics files return empty dicts.
    """
    import re as _re

    def _run_id(params: dict[str, str]) -> str:
        # Sort by key so two tasks with identical params but different
        # dict construction order group together. Without this, tasks
        # whose params dicts were built in different orders end up in
        # separate grid points.
        raw = "_".join(str(params[k]) for k in sorted(params))
        return _re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)

    # Group tasks by grid point (via run_id over params)
    groups: dict[str, list[Path]] = {}
    for task in tasks_data["tasks"].values():
        key = _run_id(task["params"])
        groups.setdefault(key, []).append(Path(task["result_dir"]))

    results: dict[str, dict] = {}
    for grid_key, result_dirs in groups.items():
        results[grid_key] = reduce_metrics(result_dirs)

    return results


def _wave_num_of(path: str | Path) -> int | None:
    """Wave number encoded in a ``wave_<N>.json`` filename, or None."""
    try:
        return int(Path(path).stem.split("_", 1)[1])
    except (ValueError, IndexError):
        return None


def reduce_partials(combiner_dir: str | Path, *, run_id: str | None = None) -> dict[str, dict]:
    """Merge per-wave partial aggregates from _combiner/wave_*.json.

    Each wave file contains ``grid_points``: a mapping of grid-point key to
    aggregated metrics for that wave.  This function merges across
    waves using the same weighted-mean logic as :func:`reduce_metrics`,
    keyed on ``n_samples``.

    Parameters
    ----------
    combiner_dir : str or Path
        Directory containing ``wave_*.json`` files (legacy-flat) and/or a
        run-scoped ``<run_id>/wave_*.json`` subdir (BR-9). Both layouts are
        read; a run-scoped partial wins over its legacy-flat twin for the same
        wave number (see :func:`_wave_partial_files`).
    run_id : str or None
        When given, the run-scoped ``<combiner_dir>/<run_id>/`` subdir is read
        (preferred over the flat layout) AND a legacy-flat wave file whose own
        ``run_id`` field names a DIFFERENT run is skipped (F05): the flat
        ``_combiner/`` is delete-protected and shared across runs at the same
        remote_path, so a prior run's partials persist and would otherwise
        contaminate this run's aggregate. Fails OPEN — a wave with no ``run_id``
        field (legacy trees / fixtures) is always reduced, and ``run_id=None``
        disables both the run-scoped scan and the filter (the historical
        behavior).

    Returns
    -------
    dict mapping grid-point key (str) to aggregated metrics (dict).
    """
    combiner_dir = Path(combiner_dir)
    wave_files = sorted(
        _wave_partial_files(combiner_dir, run_id),
        key=lambda p: int(Path(p).stem.split("_", 1)[1]),
    )

    # Collect partial entries per grid-point key across all waves
    partials: dict[str, list[dict]] = {}
    for wf in wave_files:
        try:
            with open(wf, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        file_run_id = data.get("run_id")
        if run_id is not None and file_run_id is not None and file_run_id != run_id:
            continue  # foreign partial from another run — never merge (F05)
        for grid_key, metrics in data.get("grid_points", {}).items():
            partials.setdefault(grid_key, []).append(metrics)

    # Weighted-mean aggregation per grid-point key, sharing the helper with
    # reduce_metrics so the two stay in lock-step on rounding and
    # missing-key semantics.
    return {grid_key: _weighted_mean(entries) for grid_key, entries in partials.items()}


def collect_wave_errors(
    combiner_dir: str | Path, *, run_id: str | None = None
) -> dict[int, list[str]]:
    """Map wave number → the per-task read errors the combiner recorded.

    Each ``wave_<N>.json`` carries an ``errors`` list naming tasks whose
    ``metrics.json`` could not be read. Those tasks are absent from the
    wave's ``grid_points``, so :func:`reduce_partials` silently means
    over the readable subset. A caller that presents the aggregate as
    final should consult this to know the mean was computed over a
    partial task set. Only waves with at least one error are included.

    A wave file that cannot be parsed at all (a truncated pull, a torn scp,
    a local disk-full write) is itself recorded as that wave's error (F09):
    :func:`reduce_partials` silently drops its grid_points, so without this
    the loss would be invisible AND the filename-present file would never be
    re-pulled. *run_id* skips foreign partials the same way
    :func:`reduce_partials` does — a prior run's leftover wave is not this
    run's incomplete wave.
    """
    combiner_dir = Path(combiner_dir)
    out: dict[int, list[str]] = {}
    for wf in sorted(_wave_partial_files(combiner_dir, run_id)):
        wave_num = _wave_num_of(wf)
        if wave_num is None:
            continue
        try:
            with open(wf, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            # F09: an unreadable wave file drops silently from reduce_partials —
            # surface it as this wave's error so the caller escalates and the
            # incremental pull re-fetches the intact remote copy.
            out[wave_num] = [f"wave_{wave_num}.json unreadable: {exc}"]
            continue
        file_run_id = data.get("run_id")
        if run_id is not None and file_run_id is not None and file_run_id != run_id:
            continue  # foreign partial — not this run's incomplete wave (F05)
        errs = data.get("errors") or []
        if not errs:
            continue
        out[wave_num] = [str(e) for e in errs]
    return out


def reduce_resource_usage(tasks: dict[str, dict] | dict[int, dict]) -> dict:
    """Sum per-task cpu_s / gpu_s / elapsed_s into a run-level cost rollup.

    Accepts the ``tasks`` map from a status report (string-keyed, 0-based
    HpcTaskId) or the raw ``tasks`` dict from :func:`query_sacct` /
    :func:`query_sge` (int-keyed, also 0-based after the ingest edge).
    Missing keys are treated as 0 so partial/unknown tasks do
    not crash the rollup.

    Returns a dict with stable keys::

        {
            "cpu_hours": float,   # sum(cpu_s) / 3600
            "core_hours": float,  # #345 normalized cost — identical to cpu_hours,
                                  # surfaced under the issue's cost vocabulary so
                                  # status/aggregate carry the per-run *actual* in
                                  # the same unit the pre-dispatch estimate uses
            "gpu_hours": float,   # sum(gpu_s) / 3600
            "elapsed_hours": float,  # sum(elapsed_s) / 3600 -- i.e. wall-time summed across tasks
            "tasks_counted": int, # number of tasks that contributed nonzero elapsed_s
        }

    ``core_hours`` is the per-run *actual* compute cost (#345). It equals
    ``cpu_hours`` by construction (per-task ``cpu_s`` is already
    ``cores × elapsed_s``); both names are emitted via the single
    normalization in :mod:`hpc_agent.infra.cost` so the post-run actual and
    the pre-dispatch estimate (the cost/scale gate) can never drift onto two
    different definitions of a core-hour. Additive surfacing only — no
    behavior gate lives here.
    """
    from hpc_agent.infra.cost import (
        core_hours_from_cpu_seconds,
        gpu_hours_from_gpu_seconds,
    )

    total_cpu_s = 0.0
    total_gpu_s = 0.0
    total_elapsed_s = 0.0
    counted = 0
    for info in (tasks or {}).values():
        if not isinstance(info, dict):
            continue
        # Per-task resource values can be fractional (sub-second tasks,
        # GPU-seconds during partial allocation windows). Coercing to
        # int at the task level truncates the fraction before summing
        # and silently under-counts. Sum as float; round at the end.
        elapsed = float(info.get("elapsed_s", 0) or 0)
        cpu = float(info.get("cpu_s", 0) or 0)
        gpu = float(info.get("gpu_s", 0) or 0)
        total_elapsed_s += elapsed
        total_cpu_s += cpu
        total_gpu_s += gpu
        if elapsed > 0:
            counted += 1
    core_hours = core_hours_from_cpu_seconds(total_cpu_s)
    return {
        "cpu_hours": core_hours,
        "core_hours": core_hours,
        "gpu_hours": gpu_hours_from_gpu_seconds(total_gpu_s),
        "elapsed_hours": round(total_elapsed_s / 3600.0, 4),
        "tasks_counted": counted,
    }
