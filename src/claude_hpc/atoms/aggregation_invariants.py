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

import json
from typing import TYPE_CHECKING, Any

from claude_hpc import errors
from claude_hpc._internal.primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


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
    * ``ok`` — True iff every invariant passes.

    The agent reads ``ok`` and surfaces any violations to the user.
    Pure read-only function — no SSH, no filesystem writes.

    *aggregated_metrics* + *aggregated_keying* are an opt-in extension
    that absorbs the prose 4a.3 spot-check from /aggregate-hpc.
    Supply both together; ``aggregated_keying="grid_point"`` triggers
    the keys-vs-tasks-resolve check, ``"run_id"`` skips it (the keys
    are then expected to be a single run_id string), ``None``
    disables.

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

    from claude_hpc.state.runs import read_run_sidecar

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
        from claude_hpc import load_tasks_module, tasks_path
        from claude_hpc.mapreduce.reduce.rollup import _grid_point_key

        try:
            tasks = load_tasks_module(tasks_path(experiment_dir))
            total = int(tasks.total())
            expected_grid_points = {_grid_point_key(tasks.resolve(i) or {}) for i in range(total)}
            keys = set(aggregated_metrics.keys())
            unexpected_aggregated_keys = sorted(keys - expected_grid_points)
        except (FileNotFoundError, AttributeError, TypeError, ValueError):
            # tasks.py may not be importable in the local checkout
            # (e.g. cluster-side aggregate replayed locally). Skip
            # silently — the check is opt-in and shouldn't fail the
            # parent invariant when its own dependencies aren't
            # available.
            unexpected_aggregated_keys = []

    ok = (
        all_waves_combined
        and all_tasks_present
        and provenance_present
        and not unexpected_tasks
        and not unexpected_aggregated_keys
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
        "expected_wave_count": len(expected_waves),
        "pulled_wave_count": len(pulled_waves),
        "expected_task_count": len(expected_tasks),
        "pulled_task_count": len(pulled_tasks),
    }
