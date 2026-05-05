"""Apply the cluster's survival atoms to a resubmit's override dict.

When a run fails and the agent (or the resubmit slash command) decides
to retry, today's path is purely mechanical: the static override table
in ``/monitor-hpc`` doubles memory, multiplies walltime, and hands the
result to ``runner.resubmit_failed`` as-is. The cluster's survival
atoms — ``cold_start_mem_buffer``, ``walltime_arbitrage``, and
``should_daisy_chain`` — are the same atoms ``plan_submit`` runs at
*initial-submit* time, but they never fire on resubmit. The resubmit
ends up asking for round-number resources at peak hours with no buffer
against the OOM daemon. That asymmetry is the whole "shouldn't resubmit
share submit's atoms" critique.

:func:`plan_resubmit_overrides` closes the gap by composing the
existing pure atoms:

* :func:`~claude_hpc.forecast.walltime_arbitrage.arbitrage_walltime`
  — trim the walltime ask to fit backfill shadows when no prior exists.
* ``cold_start_mem_buffer`` from :mod:`claude_hpc.infra.clusters` — grow
  the memory ask by N% when no prior exists, clamped by the cluster's
  ``max_node_mem_mb`` ceiling.
* :func:`~claude_hpc.planning.daisy_chain.should_daisy_chain` — flag
  (advisory) when the adjusted walltime exceeds the cluster's hard
  scheduler ceiling so the caller knows segmentation is required.

Cold-start detection mirrors the planner: a (profile, cluster) pair
with fewer than ``MIN_PRIOR_SAMPLES`` successful samples is treated as
cold, and the survival atoms apply. Once a prior accumulates the atoms
no-op (the prior already encodes the right safety margin via the
walltime-drift calibration loop in
:mod:`claude_hpc.forecast.calibration`).

This module is **pure** over its inputs + the runtime-prior pool on
disk. Both ``cmd_resubmit`` and any future ``resubmit_flow`` macro
should call it before handing overrides to ``runner.resubmit_failed``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from claude_hpc.forecast.walltime_arbitrage import arbitrage_walltime
from claude_hpc.infra.clusters import (
    get_auto_daisy_chain,
    get_cold_start_mem_buffer,
    get_max_node_mem_mb,
    get_max_walltime_sec,
    get_walltime_arbitrage,
    load_clusters_config,
)
from claude_hpc.planning.daisy_chain import should_daisy_chain
from claude_hpc.state.runtime_prior import read_samples

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "MIN_PRIOR_SAMPLES",
    "PlannedResubmitOverrides",
    "plan_resubmit_overrides",
]


# Threshold at which the (profile, cluster) pair stops being treated as
# cold-start. Matches the planner's ``min_samples=5`` walltime path
# (planner.py uses 10 for mem; we go with the looser walltime threshold
# since either signal is enough to skip the survival bump).
MIN_PRIOR_SAMPLES = 5


@dataclass(frozen=True)
class PlannedResubmitOverrides:
    """Output of :func:`plan_resubmit_overrides`.

    ``overrides`` is the adjusted dict the caller should hand to
    ``runner.resubmit_failed``. ``rationales`` carries one
    short-string explanation per knob the planner touched, suitable for
    surfacing in the resubmit response envelope so the agent can show
    the user *why* the ask differs from what they passed in.
    """

    overrides: dict[str, Any]
    rationales: dict[str, str] = field(default_factory=dict)
    cold_start: bool = False
    daisy_chain_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_resubmit_overrides(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    base_overrides: dict[str, Any] | None,
) -> PlannedResubmitOverrides:
    """Apply the cluster's survival atoms to *base_overrides*.

    Parameters
    ----------
    experiment_dir:
        Used to locate the runtime-prior pool that drives cold-start
        detection.
    profile, cluster:
        Bucket keys for the runtime prior. Pulled from the run sidecar
        by the caller.
    base_overrides:
        The agent's requested overrides — typically ``{"mem_mb": ...,
        "walltime_sec": ...}`` from the static ``/monitor-hpc`` table.
        ``None`` is treated as ``{}``: the planner still emits a
        cold-start verdict, but has nothing to adjust.

    Returns
    -------
    PlannedResubmitOverrides
        ``overrides`` is the input dict with any survival atoms applied.
        Keys present in ``base_overrides`` but not touched by an atom
        pass through unchanged.

    Notes
    -----
    The atom is permissive on missing cluster config: an unknown
    ``cluster`` key (e.g., a sidecar from a deprecated cluster) returns
    ``base_overrides`` unmodified with ``cold_start=False`` rather than
    raising, since failing the resubmit on a config lookup would be
    worse than skipping the survival pass.
    """
    base = dict(base_overrides or {})
    clusters = load_clusters_config()
    cluster_cfg = clusters.get(cluster)
    if cluster_cfg is None:
        return PlannedResubmitOverrides(overrides=base)

    samples = read_samples(experiment_dir, profile=profile, cluster=cluster)
    cold_start = len(samples) < MIN_PRIOR_SAMPLES

    out = dict(base)
    rationales: dict[str, str] = {}

    if cold_start and "mem_mb" in base:
        buffer = get_cold_start_mem_buffer(cluster_cfg)
        if buffer > 0:
            ceiling = get_max_node_mem_mb(cluster_cfg)
            grown = int(round(int(base["mem_mb"]) * (1.0 + buffer)))
            if ceiling is not None and grown > ceiling:
                out["mem_mb"] = ceiling
                rationales["mem_mb"] = (
                    f"cold-start +{buffer * 100:.0f}% clamped to ceiling {ceiling}MB"
                )
            elif grown != int(base["mem_mb"]):
                out["mem_mb"] = grown
                rationales["mem_mb"] = f"cold-start +{buffer * 100:.0f}% buffer"

    if cold_start and "walltime_sec" in base and get_walltime_arbitrage(cluster_cfg):
        arbitraged = arbitrage_walltime(int(base["walltime_sec"]))
        if arbitraged != int(base["walltime_sec"]):
            out["walltime_sec"] = arbitraged
            rationales["walltime_sec"] = (
                f"cold-start arbitrage {base['walltime_sec']}s→{arbitraged}s "
                f"(fits backfill shadows the round ask doesn't reach)"
            )

    daisy_chain_required = False
    if "walltime_sec" in out:
        max_wt = get_max_walltime_sec(cluster_cfg)
        if (
            max_wt is not None
            and get_auto_daisy_chain(cluster_cfg)
            and should_daisy_chain(int(out["walltime_sec"]), max_wt)
        ):
            daisy_chain_required = True
            rationales["daisy_chain"] = (
                f"walltime ask {out['walltime_sec']}s exceeds cluster max {max_wt}s "
                f"(less 1h queue-wait buffer); segmented submission required"
            )

    return PlannedResubmitOverrides(
        overrides=out,
        rationales=rationales,
        cold_start=cold_start,
        daisy_chain_required=daisy_chain_required,
    )
