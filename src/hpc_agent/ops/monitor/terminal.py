"""Terminal-state classification + runtime-prior ingestion at terminal.

Extracted from :mod:`hpc_agent.ops.monitor_flow` so the lifecycle
classifier and the post-terminal side-effect are isolated from the poll
loop. ``_is_terminal`` is pure — it inspects the polled status dict
and returns the lifecycle classification.
``_ingest_runtime_at_terminal`` performs an rsync pull and a runtime-
prior ingest as a best-effort hook.

Re-exported from :mod:`hpc_agent.ops.monitor_flow` so the helpers stay
reachable under their legacy attribute path.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any

from hpc_agent._kernel.contract.vocabulary import LifecycleState

__all__ = ["_ingest_runtime_at_terminal", "_is_terminal"]


def _ingest_runtime_at_terminal(experiment_dir: Path, *, record: Any) -> int:
    """Pull `_combiner/wave_*.runtime.json` from the cluster and ingest.

    The runtime-prior pipeline normally runs from `aggregate_flow`. This
    hook lets `monitor_flow` feed the warm-axis-picker even when the
    user never invokes `/aggregate-hpc` (e.g. they read metrics on the
    cluster directly, or only care about pass/fail). Best-effort:
    failures are swallowed — monitor's job is lifecycle, not priors.

    Pull is filtered to just the runtime sidecars (~1 file per wave,
    typically <100KB total) — cheap relative to a full `_combiner/`
    pull. Idempotent: re-running on the same run is safe because
    `append_sample` dedups on `(run_id, task_id)`.

    The pull lands under a :class:`tempfile.TemporaryDirectory` so a
    long-running monitor that ticks N runs to terminal does not leak N
    ``hpc_runtime_pull_*`` dirs under ``$TMPDIR``.
    """
    from hpc_agent import errors
    from hpc_agent.infra.transport import rsync_pull
    from hpc_agent.state.runs import read_run_sidecar
    from hpc_agent.state.runtime_prior import ingest_runtime_samples_from_combiner_dir

    try:
        with tempfile.TemporaryDirectory(prefix="hpc_runtime_pull_") as local_dir_str:
            local_dir = Path(local_dir_str)
            result = rsync_pull(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                remote_subdir="_combiner",
                local_dir=str(local_dir),
                include=["wave_*.runtime.json"],
            )
            if result.returncode != 0:
                return 0
            cmd_sha = None
            with contextlib.suppress(
                FileNotFoundError, OSError, json.JSONDecodeError, errors.HpcError
            ):
                cmd_sha = read_run_sidecar(experiment_dir, record.run_id).get("cmd_sha")
            return ingest_runtime_samples_from_combiner_dir(
                local_dir,
                experiment_dir=experiment_dir,
                profile=record.profile,
                cluster=record.cluster,
                cmd_sha=cmd_sha,
            )
    except (OSError, TimeoutError):
        return 0


def _is_terminal(
    last_status: dict[str, Any],
    total_tasks: int,
    *,
    partial_ok: bool = False,
) -> tuple[str | None, str | None]:
    """Inspect counts and return (lifecycle_state, escalation_reason).

    Returns ``(None, None)`` when still in flight.

    With ``partial_ok=True``, the wave is classified ``complete`` as
    soon as no work is left and at least one task succeeded. Only a
    zero-success wave is classified ``failed`` under partial-ok.
    """
    complete = int(last_status.get("complete", 0))
    running = int(last_status.get("running", 0))
    pending = int(last_status.get("pending", 0))
    failed = int(last_status.get("failed", 0))

    if complete >= total_tasks:
        return (LifecycleState.COMPLETE, None)
    if running == 0 and pending == 0 and failed > 0:
        if partial_ok and complete > 0:
            # Partial success: at least one task done, no work left.
            return (LifecycleState.COMPLETE, "partial_ok_with_failures")
        # No work left and at least one failure. MVP doesn't auto-resubmit;
        # surface the failure for the caller to handle.
        return (LifecycleState.FAILED, "failed_tasks_no_auto_recover_in_mvp")
    return (None, None)
