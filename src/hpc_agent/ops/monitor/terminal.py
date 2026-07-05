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

from hpc_agent.ops.monitor.classify import classify_polling

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
    unknown_streak: int = 0,
) -> tuple[str | None, str | None]:
    """Inspect counts and return (lifecycle_state, escalation_reason).

    Returns ``(None, None)`` when still in flight.

    With ``partial_ok=True``, the wave is classified ``complete`` as
    soon as no work is left and at least one task succeeded. Only a
    zero-success wave is classified ``failed`` under partial-ok.

    ``unknown_streak`` is the poll loop's count of consecutive
    unresolved-unknown ticks (see ``classify.unresolved_unknown``); at
    ``UNKNOWN_TICKS_BEFORE_ESCALATION`` the classifier escalates to a
    terminal ``abandoned`` anomaly instead of polling unknown forever
    (the vanished-workdir class, proving run #3 finding f). Callers that
    do not track a streak (the aggregate precondition) leave the default
    ``0`` and never trigger the arm.

    Thin adapter over the shared mid-flight classifier so the monitor poll
    loop and the reconcile settle path read the same count-to-verdict rule
    from one place (:mod:`hpc_agent.ops.monitor.classify`).
    """
    return classify_polling(
        last_status, total_tasks, partial_ok=partial_ok, unknown_streak=unknown_streak
    )
