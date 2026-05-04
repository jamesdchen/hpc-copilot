"""``walltime-drift`` primitive — recommend a safety-mult adjustment.

Pure-dispatch primitive: reads runtime samples from the local journal,
runs the calibration analysis, and projects the result onto the
envelope shape. No SSH, no scheduler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_hpc._internal._primitive import primitive
from slash_commands import errors


@primitive(
    name="walltime-drift",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
)
def walltime_drift(
    *,
    experiment_dir: Path,
    profile: str,
    cluster: str,
    cmd_sha: str | None = None,
    base_safety_mult: float,
) -> dict[str, Any]:
    """Return the walltime-drift summary + recommended safety_mult.

    Reads recent samples (successful and failed) for the given profile,
    cluster, and optional cmd_sha; computes drift signals (cliff rate,
    near-miss count, median utilisation) and recommends an adjustment
    to ``base_safety_mult``.
    """
    from claude_hpc.orchestrator.calibration import (
        compute_walltime_drift,
        recommend_safety_mult_adjustment,
    )
    from claude_hpc.orchestrator.runtime_prior import read_samples

    samples = read_samples(
        experiment_dir,
        profile=profile,
        cluster=cluster,
        cmd_sha=cmd_sha,
        only_successful=False,
    )
    drift = compute_walltime_drift(samples)
    adjusted, rationale = recommend_safety_mult_adjustment(
        drift, base_safety_mult=float(base_safety_mult)
    )
    return {
        "n_recent": drift.n_recent,
        "n_cliff_events": drift.n_cliff_events,
        "n_near_misses": drift.n_near_misses,
        "weighted_cliff_rate": drift.weighted_cliff_rate,
        "median_utilization": drift.median_utilization,
        "base_safety_mult": float(base_safety_mult),
        "adjusted_safety_mult": adjusted,
        "rationale": rationale,
    }
