"""``validate``: pre-submit timing probe for a resource ask.

Promotes the internal ``--test-only`` probe (used by the planner's score
endpoint) to a first-class CLI primitive. MARs and other agents can
branch on submission timing — *fits in the 30-minute backfill window?*
*queue is 6 hours deep, postpone?* — without committing to an actual
submit. Pattern borrowed from LARA-HPC's "validation-first" submit flow.

Idempotent on the resource ask: the scheduler\'s ``--test-only`` mode
never enqueues a job; it only returns the predicted start time. Repeated
calls have no side effect beyond the SSH probe itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc.infra.clusters import load_clusters_config

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["validate_submission"]


# Reasonable backfill horizon — any predicted start under this counts as
# "fits backfill". 600s (10 minutes) matches SLURM\'s default
# bf_window=720 minutes / 72 buckets ≈ 10-minute cells; tunable via the
# kwarg if a cluster runs with a different backfill granularity.
DEFAULT_BACKFILL_WINDOW_SEC = 600


@primitive(
    name="validate",
    verb="validate",
    side_effects=[SideEffect("ssh", "<cluster> (scheduler --test-only probe)")],
    idempotent=True,
)
def validate_submission(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    walltime_sec: int,
    mem_mb: int,
    cpus: int,
    constraint: str | None = None,
    gpus: int = 0,
    backfill_window_sec: int = DEFAULT_BACKFILL_WINDOW_SEC,
) -> dict[str, Any]:
    """Probe the scheduler\'s ``--test-only`` to predict submission timing.

    Returns a dict pinned by ``schemas/validate.output.json``:

    * ``estimated_start_iso``: the scheduler\'s predicted start time, or
      ``None`` when the probe didn\'t parse / scheduler unsupported.
    * ``predicted_eta_sec``: same as the planner\'s ``eta_sec_via_test_only``
      — seconds from now until predicted start, or ``None``.
    * ``fits_backfill``: True when ``predicted_eta_sec`` is under
      ``backfill_window_sec`` (default 600s). MARs key off this to decide
      whether to submit now or hold back.
    * ``reason``: human-readable summary.
    * ``scheduler_response``: raw probe stdout (clamped) for debugging.

    Errors raise no exceptions for cluster-side failures (scheduler
    throttled, ssh timeout); they surface as ``predicted_eta_sec=None``
    with a descriptive ``reason``. Hard failures (config missing, profile
    unknown) propagate as ``ValueError``.
    """
    cfg = load_clusters_config()
    cluster_cfg = (cfg.get("clusters") or {}).get(cluster)
    if cluster_cfg is None:
        raise ValueError(f"unknown cluster {cluster!r}; not in clusters.yaml")
    scheduler = cluster_cfg.get("scheduler", "slurm")

    # Re-export the planner\'s probe to keep behaviour identical (mem
    # flag formatting, walltime formatting, parse regex). If the planner
    # ever switches probe implementations, validate inherits the change
    # for free.
    from claude_hpc.planning.planner import _eta_via_test_only_with_resources

    eta_sec, raw_text = _eta_via_test_only_with_resources(
        scheduler,
        cluster_cfg,
        constraint=constraint or "<cpu-only>",
        walltime_sec=int(walltime_sec),
        mem_mb=int(mem_mb),
        cpus=int(cpus),
    )

    if eta_sec is None:
        reason = (
            f"scheduler {scheduler!r} did not return a parseable start time "
            f"(throttled, unsupported, or non-SLURM)"
        )
        fits = False
        estimated_start_iso: str | None = None
    else:
        from claude_hpc._internal._time import utcnow

        ts = utcnow().timestamp() + int(eta_sec)
        from datetime import datetime, timezone

        estimated_start_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        fits = int(eta_sec) <= int(backfill_window_sec)
        verdict = "fits" if fits else "exceeds"
        reason = (
            f"predicted start in {int(eta_sec)}s ({verdict} {int(backfill_window_sec)}s window)"
        )

    return {
        "profile": profile,
        "cluster": cluster,
        "scheduler": scheduler,
        "estimated_start_iso": estimated_start_iso,
        "predicted_eta_sec": eta_sec,
        "fits_backfill": bool(fits),
        "backfill_window_sec": int(backfill_window_sec),
        "reason": reason,
        "scheduler_response": (raw_text or "")[:2000],
    }
