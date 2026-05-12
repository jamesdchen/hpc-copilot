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

from typing import TYPE_CHECKING

from claude_hpc._internal.primitive import SideEffect, primitive
from claude_hpc._schema_models.validators.validate import ValidateResult, ValidateSpec
from claude_hpc.infra.clusters import load_clusters_config

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["validate_submission"]


@primitive(
    name="validate",
    verb="validate",
    side_effects=[SideEffect("ssh", "<cluster> (scheduler --test-only probe)")],
    idempotent=True,
    agent_facing=True,
)
def validate_submission(experiment_dir: Path, *, spec: ValidateSpec) -> ValidateResult:
    """Probe the scheduler's ``--test-only`` to predict submission timing.

    Returns a :class:`ValidateResult` whose ``model_dump(mode="json")``
    matches ``schemas/validate.output.json``.

    * ``estimated_start_iso``: the scheduler's predicted start time, or
      ``None`` when the probe didn't parse / scheduler unsupported.
    * ``predicted_eta_sec``: seconds from now until predicted start, or
      ``None``.
    * ``fits_backfill``: True when ``predicted_eta_sec`` is under
      ``spec.backfill_window_sec``. MARs key off this to decide whether
      to submit now or hold back.
    * ``reason``: human-readable summary.
    * ``scheduler_response``: raw probe stdout (clamped) for debugging.

    Errors raise no exceptions for cluster-side failures (scheduler
    throttled, ssh timeout); they surface as ``predicted_eta_sec=None``
    with a descriptive ``reason``. Hard failures (config missing, profile
    unknown) propagate as ``ValueError``.
    """
    cfg = load_clusters_config()
    # load_clusters_config returns a flat {cluster_name: {...}} dict;
    # planner.py and resubmit_planner.py both index it directly.
    cluster_cfg = cfg.get(spec.cluster)
    if cluster_cfg is None:
        raise ValueError(f"unknown cluster {spec.cluster!r}; not in clusters.yaml")
    scheduler = cluster_cfg.get("scheduler", "slurm")

    # Re-export the planner's probe to keep behaviour identical (mem
    # flag formatting, walltime formatting, parse regex). If the planner
    # ever switches probe implementations, validate inherits the change
    # for free.
    from claude_hpc.planning.planner import _eta_via_test_only_with_resources

    eta_sec, raw_text = _eta_via_test_only_with_resources(
        scheduler,
        cluster_cfg,
        constraint=spec.constraint or "<cpu-only>",
        walltime_sec=spec.walltime_sec,
        mem_mb=spec.mem_mb,
        cpus=spec.cpus,
    )

    if eta_sec is None:
        reason = (
            f"scheduler {scheduler!r} did not return a parseable start time "
            f"(throttled, unsupported, or non-SLURM)"
        )
        fits = False
        estimated_start_iso: str | None = None
    else:
        from datetime import datetime, timezone

        from claude_hpc._internal.time import utcnow

        ts = utcnow().timestamp() + int(eta_sec)
        estimated_start_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        fits = int(eta_sec) <= spec.backfill_window_sec
        verdict = "fits" if fits else "exceeds"
        reason = (
            f"predicted start in {int(eta_sec)}s ({verdict} {spec.backfill_window_sec}s window)"
        )

    return ValidateResult(
        profile=spec.profile,
        cluster=spec.cluster,
        scheduler=scheduler,
        estimated_start_iso=estimated_start_iso,
        predicted_eta_sec=eta_sec,
        fits_backfill=bool(fits),
        backfill_window_sec=spec.backfill_window_sec,
        reason=reason,
        scheduler_response=(raw_text or "")[:2000],
    )
