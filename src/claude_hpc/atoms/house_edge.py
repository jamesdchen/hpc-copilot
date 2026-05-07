"""``house-edge`` primitive — calibration delta vs. predicted runtime.

Pure-dispatch primitive: reads successful runtime samples from the
local journal and projects the calibration summary onto the envelope
shape. No SSH, no scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_hpc import errors
from claude_hpc._internal.primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="house-edge",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli="hpc-mapreduce house-edge --profile <name> --cluster <name> [--cmd-sha <sha>]",
)
def house_edge(
    *,
    experiment_dir: Path,
    profile: str,
    cluster: str,
    cmd_sha: str | None = None,
) -> dict[str, Any]:
    """Return the calibration summary (delta seconds + ratio).

    Reads only successful samples — the calibration we care about is
    over runs that finished, not failed/cancelled ones.
    """
    from claude_hpc.forecast.calibration import compute_house_edge
    from claude_hpc.state.runtime_prior import read_samples

    samples = read_samples(
        experiment_dir,
        profile=profile,
        cluster=cluster,
        cmd_sha=cmd_sha,
        only_successful=True,
    )
    edge = compute_house_edge(samples)
    return {
        "n_with_prediction": edge.n_with_prediction,
        "mean_delta_sec": edge.mean_delta_sec,
        "median_delta_sec": edge.median_delta_sec,
        "p95_delta_sec": edge.p95_delta_sec,
        "calibration_ratio": edge.calibration_ratio,
    }
