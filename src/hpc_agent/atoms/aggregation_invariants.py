"""``verify-aggregation-complete`` primitive — invariant check post-aggregate.

Replaces the prose at /aggregate-hpc that asks the agent to "verify
every task is accounted for, every wave combined, provenance present"
by walking the artifacts. The user's aggregation OUTPUT is opaque
(framework doesn't know what `qlike_score=0.42` means), but the
INVARIANTS around it (every task ran, every wave's combiner partial
exists, no orphan task IDs) are framework-knowable.

The primitive walks the local pulled ``_combiner/`` dir + the run
sidecar's wave_map and reports any drift the agent then frames for
the user.
"""

from __future__ import annotations

import csv
import json
import math
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._internal.primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


def _is_nan(value: str) -> bool:
    """Return True when *value* is an empty cell or a NaN-like float string.

    A result-file metric cell is "missing" when it is blank, or when it
    parses to a float that is NaN. Non-numeric strings that are not
    blank (e.g. an error token) are also treated as missing — a metric
    column is expected to hold a real number.
    """
    stripped = value.strip()
    if not stripped:
        return True
    try:
        return math.isnan(float(stripped))
    except (TypeError, ValueError):
        return True


def check_result_columns(
    results_dir: Path | str,
    *,
    expected_columns: list[str] | None = None,
    metric_column: str | None = None,
    file_glob: str = "*.csv",
) -> dict[str, Any]:
    """Verify CSV result files under *results_dir* against a declared schema.

    Deterministic, pure-code check — no LLM. For every ``*.csv`` (matched
    by *file_glob*) found anywhere under *results_dir*:

    * ``expected_columns`` — every declared column name must be present
      in the file's header row. Missing names land in ``missing_columns``.
    * ``metric_column`` — the named column must exist and every data row
      must carry a non-empty, non-NaN numeric value. A blank cell, a
      non-numeric token, or a NaN float is a violation (``metric_nan``).

    When neither *expected_columns* nor *metric_column* is declared the
    check is a clean no-op: ``checked=False``, empty ``violations``.

    Returns a dict::

        {
          "checked": bool,            # True iff a schema was declared
          "ok": bool,                 # True iff no violations
          "files_scanned": int,
          "violations": [             # one entry per offending file
            {"path": str,
             "missing_columns": list[str],
             "metric_nan": bool,
             "metric_nan_rows": list[int],   # 1-based data-row indices
             "error": str | None},
          ],
        }
    """
    from pathlib import Path as _Path

    declared = bool(expected_columns) or bool(metric_column)
    rdir = _Path(results_dir)
    violations: list[dict[str, Any]] = []
    files_scanned = 0

    if not declared:
        return {"checked": False, "ok": True, "files_scanned": 0, "violations": []}

    expected = list(expected_columns or [])
    paths = sorted(p for p in rdir.rglob(file_glob) if p.is_file() and "_wip_" not in str(p))
    for path in paths:
        files_scanned += 1
        missing_columns: list[str] = []
        metric_nan_rows: list[int] = []
        error: str | None = None
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = next(reader, None)
                if header is None:
                    error = "empty file: no header row"
                else:
                    header_set = set(header)
                    missing_columns = [c for c in expected if c not in header_set]
                    metric_idx: int | None = None
                    if metric_column is not None:
                        if metric_column in header:
                            metric_idx = header.index(metric_column)
                        else:
                            # Surface as a missing column too so the
                            # caller sees one coherent violation list.
                            if metric_column not in missing_columns:
                                missing_columns.append(metric_column)
                    if metric_idx is not None:
                        for row_no, row in enumerate(reader, start=1):
                            if metric_idx >= len(row) or _is_nan(row[metric_idx]):
                                metric_nan_rows.append(row_no)
        except OSError as exc:
            error = f"unreadable: {exc}"

        if missing_columns or metric_nan_rows or error is not None:
            violations.append(
                {
                    "path": str(path),
                    "missing_columns": missing_columns,
                    "metric_nan": bool(metric_nan_rows),
                    "metric_nan_rows": metric_nan_rows,
                    "error": error,
                }
            )

    return {
        "checked": True,
        "ok": not violations,
        "files_scanned": files_scanned,
        "violations": violations,
    }


@primitive(
    name="verify-aggregation-complete",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="run_id",
    cli="hpc-agent verify-aggregation-complete --experiment-dir <path> --run-id <id> --combiner-dir <path>",  # noqa: E501
)
def verify_aggregation_complete(
    experiment_dir: Path,
    *,
    run_id: str,
    combiner_dir_local: Path | str,
    aggregated_metrics: dict[str, Any] | None = None,
    aggregated_keying: str | None = None,
    results_dir_local: Path | str | None = None,
) -> dict[str, Any]:
    """Verify the post-aggregate invariants for *run_id* against *combiner_dir_local*.

    Walks the run sidecar's ``wave_map`` + the locally-pulled
    ``_combiner/`` directory and reports:

    * ``all_waves_combined`` — every wave_id in the wave_map has a
      matching ``wave_<N>.json`` partial on disk locally.
    * ``missing_waves`` — wave_ids on the wave_map that don't have a
      partial pulled. Empty list when ``all_waves_combined``.
    * ``all_tasks_present`` — every task_id reachable from the
      wave_map appears in at least one of the pulled
      ``wave_<N>.json`` partials' ``task_ids`` field.
    * ``missing_tasks`` — task_ids in the wave_map but not in any
      partial.
    * ``unexpected_tasks`` — task_ids in some partial but NOT in the
      wave_map (a sign of cross-run contamination).
    * ``provenance_present`` — every wave partial has the expected
      ``run_id`` + ``wave`` provenance fields.
    * ``unexpected_aggregated_keys`` — when *aggregated_metrics* is
      supplied with *aggregated_keying = "grid_point"*, keys present
      in the dict but absent from the set of grid-point keys produced
      by ``tasks.resolve(i)`` for ``i ∈ [0, total_tasks)``. A non-
      empty list is a contamination red flag (the same bug class as
      ``unexpected_tasks`` but at the post-reduce layer). Empty list
      when the check was not run (no aggregated_metrics or wrong
      keying).
    * ``columns_checked`` / ``column_violations`` — when *results_dir_local*
      is given AND the run sidecar's ``results`` block declares
      ``expected_columns`` and/or ``metric_column``, every CSV result
      file under that directory is verified to (a) carry every declared
      column in its header and (b) hold a non-empty, non-NaN value in
      the metric column for every data row. ``columns_checked`` is False
      and ``column_violations`` empty when no schema is declared or no
      results directory was supplied — a clean no-op skip.
    * ``ok`` — True iff every invariant passes.

    The agent reads ``ok`` and surfaces any violations to the user.
    Pure read-only function — no SSH, no filesystem writes.

    *aggregated_metrics* + *aggregated_keying* are an opt-in extension
    that absorbs the prose 4a.3 spot-check from /aggregate-hpc.
    Supply both together; ``aggregated_keying="grid_point"`` triggers
    the keys-vs-tasks-resolve check, ``"run_id"`` skips it (the keys
    are then expected to be a single run_id string), ``None``
    disables.

    *results_dir_local* is the locally-pulled per-task results tree
    (e.g. aggregate-flow's ``summaries_dir_local``). Supplying it
    enables the deterministic columns / non-NaN-metric gate against the
    schema declared in the sidecar's ``results`` block.

    Raises
    ------
    :class:`errors.SpecInvalid`
        Empty *run_id* or *combiner_dir_local* not a real directory.
    """
    if not run_id:
        raise errors.SpecInvalid("run_id is required")
    from pathlib import Path as _Path

    combiner_dir = _Path(combiner_dir_local)
    if not combiner_dir.is_dir():
        raise errors.SpecInvalid(f"combiner_dir_local is not a directory: {combiner_dir}")

    from hpc_agent.state.runs import read_run_sidecar

    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(str(exc)) from exc

    wave_map: dict[str, list[int]] = sidecar.get("wave_map") or {}
    expected_tasks: set[int] = set()
    for tids in wave_map.values():
        for tid in tids:
            try:
                expected_tasks.add(int(tid))
            except (TypeError, ValueError):
                continue

    # Walk the pulled partials.
    pulled_waves: dict[int, dict[str, Any]] = {}
    for path in sorted(combiner_dir.glob("wave_*.json")):
        # Skip the runtime sidecar (wave_<N>.runtime.json).
        if path.name.endswith(".runtime.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        wave = data.get("wave")
        if isinstance(wave, int):
            pulled_waves[wave] = data

    expected_waves = {int(k) for k in wave_map if str(k).isdigit()}
    missing_waves = sorted(expected_waves - set(pulled_waves.keys()))
    all_waves_combined = not missing_waves

    pulled_tasks: set[int] = set()
    provenance_present = True
    for w, doc in pulled_waves.items():
        # Provenance check: each partial must self-identify.
        if doc.get("run_id") != run_id or doc.get("wave") != w:
            provenance_present = False
        for tid in doc.get("task_ids") or []:
            try:
                pulled_tasks.add(int(tid))
            except (TypeError, ValueError):
                continue

    missing_tasks = sorted(expected_tasks - pulled_tasks)
    unexpected_tasks = sorted(pulled_tasks - expected_tasks)
    all_tasks_present = not missing_tasks

    # Aggregate-keys invariant: when the caller supplied the reduced
    # metrics dict + declared its keying as grid-point, check that
    # every key matches a grid-point produced by tasks.resolve(i) for
    # i in [0, total_tasks). Mirrors the prose 4a.3 spot-check from
    # /aggregate-hpc — same bug class as unexpected_tasks, but at the
    # post-reduce layer.
    unexpected_aggregated_keys: list[str] = []
    if aggregated_metrics is not None and aggregated_keying == "grid_point":
        import re as _re

        from hpc_agent import load_tasks_module, tasks_path

        # ``aggregated_metrics`` keys are produced by
        # ``reduce_by_grid_point._run_id`` (bare-values, sanitised). The
        # rollup helper ``rollup._grid_point_key`` uses a different
        # ``k=v`` format intended for human-readable rollup tables; using
        # it here made every key look "unexpected". Mirror the metrics
        # format inline so the check actually compares like-for-like.
        def _metrics_key(params: dict[str, object]) -> str:
            raw = "_".join(str(params[k]) for k in sorted(params))
            return _re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)

        try:
            tasks = load_tasks_module(tasks_path(experiment_dir))
            total = int(tasks.total())
            expected_grid_points = {_metrics_key(tasks.resolve(i) or {}) for i in range(total)}
            keys = set(aggregated_metrics.keys())
            unexpected_aggregated_keys = sorted(keys - expected_grid_points)
        except (FileNotFoundError, AttributeError, TypeError, ValueError):
            # tasks.py may not be importable in the local checkout
            # (e.g. cluster-side aggregate replayed locally). Skip
            # silently — the check is opt-in and shouldn't fail the
            # parent invariant when its own dependencies aren't
            # available.
            unexpected_aggregated_keys = []

    # Expected-columns / non-NaN-metric gate. Deterministic given a
    # declared schema in the sidecar's ``results`` block; a clean no-op
    # (columns_checked=False) when no schema is declared or no local
    # results directory was supplied.
    columns_checked = False
    column_violations: list[dict[str, Any]] = []
    results_block = sidecar.get("results")
    if isinstance(results_block, dict) and results_dir_local is not None:
        raw_cols = results_block.get("expected_columns")
        expected_columns = [str(c) for c in raw_cols] if isinstance(raw_cols, list) else []
        raw_metric = results_block.get("metric_column")
        metric_column = raw_metric if isinstance(raw_metric, str) and raw_metric else None
        if expected_columns or metric_column:
            results_dir = _Path(results_dir_local)
            if results_dir.is_dir():
                col_report = check_result_columns(
                    results_dir,
                    expected_columns=expected_columns,
                    metric_column=metric_column,
                )
                columns_checked = bool(col_report["checked"])
                column_violations = list(col_report["violations"])

    ok = (
        all_waves_combined
        and all_tasks_present
        and provenance_present
        and not unexpected_tasks
        and not unexpected_aggregated_keys
        and not column_violations
    )

    return {
        "ok": ok,
        "run_id": run_id,
        "all_waves_combined": all_waves_combined,
        "missing_waves": missing_waves,
        "all_tasks_present": all_tasks_present,
        "missing_tasks": missing_tasks,
        "unexpected_tasks": unexpected_tasks,
        "unexpected_aggregated_keys": unexpected_aggregated_keys,
        "provenance_present": provenance_present,
        "columns_checked": columns_checked,
        "column_violations": column_violations,
        "expected_wave_count": len(expected_waves),
        "pulled_wave_count": len(pulled_waves),
        "expected_task_count": len(expected_tasks),
        "pulled_task_count": len(pulled_tasks),
    }
