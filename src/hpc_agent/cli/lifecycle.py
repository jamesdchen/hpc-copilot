"""Lifecycle verb argparse adapters — handler home for Tier 2 lifecycle primitives.

Phase-2 split (lifecycle domain): the single hand-written ``cmd_status``
adapter that doesn't fit the registry-driven dispatcher (composite
envelope folds the run record + sidecar preempt summary + optional
``campaign_id``) lives here. The Tier 1 lifecycle primitives
(``monitor-flow``, ``monitor-summary``, ``decide-monitor-arm``,
``logs``, ``failures``) declare ``cli=CliShape(...)`` on their
``@primitive`` decorator and flow through
:func:`hpc_agent.cli._dispatch.dispatch_primitive`.

The helper :func:`_preempted_summary_from_sidecar` lives here too — it
is exclusively used by :func:`cmd_status` to surface preempted-task
counts on the ``/status`` envelope.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors, runner
from hpc_agent._internal import session
from hpc_agent.cli._helpers import EXIT_OK, _ok, _require_ssh_agent
from hpc_agent.ops.monitor.list_in_flight import _last_status_age_seconds

if TYPE_CHECKING:
    pass


def cmd_status(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.ops.monitor.status.record_status.

    Composite envelope: loads the run record, calls ``record_status``
    (refreshes the journal's ``last_status``), and folds in
    ``campaign_id`` (when set) + ``preempted_count`` /
    ``preempted_task_ids`` (from the sidecar's per-task ``preempt``
    blocks written by dispatch.py's SIGTERM handler) so the harness can
    see scheduler pressure on a partially-bumped run without first
    calling ``/failures``.
    """
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(
            f"no journal record for run_id {args.run_id!r} in {args.experiment_dir}"
        )
    updated = runner.record_status(
        args.experiment_dir,
        args.run_id,
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        job_ids=record.job_ids,
        job_name=record.job_name,
        min_rows=getattr(args, "min_rows", 0),
    )
    data: dict[str, Any] = {
        "run_id": updated.run_id,
        "lifecycle_state": updated.status,
        "last_status": updated.last_status,
        "last_status_age_seconds": _last_status_age_seconds(updated.last_status),
        "combined_waves": updated.combined_waves,
        "failed_waves": updated.failed_waves,
    }
    # Surface the campaign tag so a caller seeing /status output knows
    # this run is part of a closed-loop campaign without separately
    # querying `campaign list` / `campaign status`.
    if updated.campaign_id:
        data["campaign_id"] = updated.campaign_id

    # A-M1: surface preempted-task counts directly on /status so a
    # caller polling a partially-bumped run sees them without first
    # having to call /failures. The campus user's harness can branch
    # on "X of N tasks got preempted" while the run is still in
    # flight, instead of waiting for the whole array to fail before
    # noticing scheduler pressure. Sourced from the per-task sidecar
    # ``preempt`` block written by dispatch.py's SIGTERM handler.
    preempt_summary = _preempted_summary_from_sidecar(args.experiment_dir, args.run_id)
    if preempt_summary is not None:
        count, ids = preempt_summary
        data["preempted_count"] = count
        data["preempted_task_ids"] = ids

    _ok(data, name="poll-run-status")
    return EXIT_OK


def _preempted_summary_from_sidecar(
    experiment_dir: Any, run_id: str
) -> tuple[int, list[int]] | None:
    """Return (preempted_count, preempted_task_ids_sorted) or None.

    Walks the per-task ``tasks`` block of the run sidecar and collects
    every task_id whose entry carries a ``preempt`` block (set by
    dispatch.py's SIGTERM handler when the cluster bumps the campus
    user's low-priority job). Returns None when there are no preempted
    tasks or when the sidecar can't be read — callers should treat
    None as "no preempt info to surface", not an error.
    """
    try:
        from hpc_agent.state.runs import (
            read_run_sidecar as _read_sidecar_for_status,
        )

        sidecar = _read_sidecar_for_status(Path(experiment_dir), run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(sidecar, dict):
        return None
    tasks_block = sidecar.get("tasks") or {}
    if not isinstance(tasks_block, dict):
        return None
    preempted_ids: list[int] = []
    for tid_str, entry in tasks_block.items():
        if not isinstance(entry, dict):
            continue
        if "preempt" in entry:
            try:
                preempted_ids.append(int(tid_str))
            except (TypeError, ValueError):
                continue
    if not preempted_ids:
        return None
    return len(preempted_ids), sorted(preempted_ids)


__all__ = ["cmd_status", "_preempted_summary_from_sidecar"]
