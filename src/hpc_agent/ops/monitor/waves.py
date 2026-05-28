"""Wave-completion detection + the failure / partial-ok sibling markers.

Extracted from :mod:`hpc_agent.ops.monitor_flow` so wave-bookkeeping
gets a single home. The two persistence helpers (``_read_partial_ok``,
``_write_failed_task_ids``) own the sibling-file shape; the detection
helper (``_newly_complete_waves``) is pure compute over the polled
status dict.

Re-exported from :mod:`hpc_agent.ops.monitor_flow` so the helpers stay
reachable under their legacy attribute path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["_newly_complete_waves", "_read_partial_ok", "_write_failed_task_ids"]


def _newly_complete_waves(
    *,
    last_status: dict[str, Any],
    wave_map: dict[str, list[int]] | None,
    already_combined: set[int],
) -> list[int]:
    """Identify waves whose every task reports complete and aren't yet combined.

    The cluster-side reporter optionally emits a ``waves`` block in
    ``last_status`` when the sidecar carried a ``wave_map``. We trust
    that: when ``waves[N].complete == waves[N].total``, wave ``N`` is
    done. Falls back to "no wave_map → no combining" silently.
    """
    waves_block = last_status.get("waves")
    if not isinstance(waves_block, dict):
        return []
    # Restrict to waves the local wave_map declared so a cluster-side
    # reporter that picks up unexpected wave numbers (e.g. from a stale
    # status report, or after a fresh resubmission added new groups) can't
    # trigger combine_wave on waves the framework doesn't track.
    declared_waves: set[int] | None = None
    if wave_map is not None:
        declared_waves = set()
        for k in wave_map:
            try:
                declared_waves.add(int(k))
            except (TypeError, ValueError):
                continue
    out: list[int] = []
    for k, counts in waves_block.items():
        try:
            wave_num = int(k)
        except (TypeError, ValueError):
            continue
        if wave_num in already_combined:
            continue
        if declared_waves is not None and wave_num not in declared_waves:
            continue
        if not isinstance(counts, dict):
            continue
        # Coerce to int explicitly so a missing/None counter doesn't
        # falsy-skip a legitimate (e.g. total=5, complete=5) match, and
        # require total > 0 explicitly so empty waves don't loop until
        # walltime budget.
        try:
            total = int(counts.get("total") or 0)
            complete = int(counts.get("complete") or 0)
        except (TypeError, ValueError):
            continue
        if total > 0 and complete == total:
            out.append(wave_num)
    return sorted(out)


def _read_partial_ok(experiment_dir: Path, run_id: str) -> bool:
    """Read the partial_ok sibling marker written by submit-flow.

    Returns True iff ``<exp>/.hpc/runs/<run_id>.partial_ok`` exists.
    The marker is a sibling of the run sidecar (intentionally not a
    sidecar field) so the sidecar's frozen schema does not need to bump
    for this opt-in flag. See ``submit_flow.partial_ok``.
    """
    from hpc_agent.state.runs import run_sidecar_path

    marker = run_sidecar_path(experiment_dir, run_id).with_suffix(".partial_ok")
    return marker.is_file()


def _write_failed_task_ids(
    experiment_dir: Path,
    run_id: str,
    *,
    failed_task_ids: list[int],
    classifier_codes: list[str] | None = None,
    wave: int | None = None,
) -> None:
    """Persist the failure ledger consulted by aggregate-flow.

    Writes ``<exp>/.hpc/runs/<run_id>.failed.json`` with the shape
    documented in the D2b primitive doc — kept on disk (not in the
    sidecar) so aggregate-flow can read it without a sidecar parse.

    Routed through :func:`atomic_write_json` so a concurrent reader
    (aggregate-flow scanning the ledger) never observes a partial JSON
    write — without this, a Python-level ``write_text`` produces a
    truncate-then-write sequence that can land mid-payload.
    """
    from hpc_agent.infra.io import atomic_write_json
    from hpc_agent.state.runs import run_sidecar_path

    target = run_sidecar_path(experiment_dir, run_id).with_suffix(".failed.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "failed_task_ids": sorted(set(int(t) for t in failed_task_ids)),  # noqa: C401 — preserve original shape pre-PR-3
        "wave": wave,
        "classifier_codes": list(classifier_codes or []),
    }
    atomic_write_json(target, payload)
