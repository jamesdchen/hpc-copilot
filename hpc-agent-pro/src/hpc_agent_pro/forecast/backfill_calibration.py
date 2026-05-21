"""House-edge calibration helpers extracted from :mod:`backfill` for navigability.

The main ``backfill`` module owns lattice construction, walltime / mem /
cpu rightsizing, and the ``--test-only`` probe loop. This module owns
the post-probe calibration step: take a list of ``BackfillProbe``
results, attach a "house-edge" multiplier from the per-(profile, cluster)
calibration prior, and surface ``CalibratedProbe`` records the planner
ranks. Splitting the file means the ranking logic doesn't have to live
alongside the lattice / probe core.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

from hpc_agent_pro.forecast.backfill import BackfillProbe

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)


# ─── house-edge calibration ─────────────────────────────────────────────────
#
# SLURM's ``sbatch --test-only`` predicts when a job *would* start given
# the current queue snapshot. It models the priority queue but not
# backfill opportunism — asking for an "inferior" resource (fewer GPUs,
# weaker type) routinely slots into a shadow the predictor can't see, so
# the raw ETA is systematically pessimistic on cheap asks and optimistic
# on contended flagship pools. The runtime-prior pool already captures
# (predicted_eta_sec, actual_queue_wait_sec) pairs via house-edge
# tracking; ``calibrate_probes`` feeds that signal back into the lattice
# rank so ``pick_earliest_calibrated`` picks by what the cluster *will*
# do, not what the scheduler *says* it will do.

CALIBRATION_FACTOR_FLOOR = 0.1
CALIBRATION_FACTOR_CEILING = 10.0


@dataclasses.dataclass(frozen=True)
class CalibratedProbe:
    """A backfill probe with its house-edge-adjusted ETA.

    Carries the raw probe alongside the calibrated values so the planner
    report can surface both — the slash command can show *"predicted
    300s, calibrated 1100s based on 23 prior runs (factor 3.7×)"* rather
    than silently rewriting the scheduler's number.
    """

    probe: BackfillProbe
    eta_sec_calibrated: int | None
    factor: float | None  # the ratio actually applied; None when no calibration data
    gpu_types: tuple[str, ...]


def calibrate_probes(
    probes: list[BackfillProbe],
    *,
    edges_by_gpu_type: dict[str, Any],
    gpu_types_for_constraint: Callable[[str], list[str]],
    floor: float = CALIBRATION_FACTOR_FLOOR,
    ceiling: float = CALIBRATION_FACTOR_CEILING,
) -> list[CalibratedProbe]:
    """Apply per-GPU-type house-edge factors to each probe's ETA.

    *edges_by_gpu_type* is the dict returned by
    :func:`~hpc_agent_pro.forecast.calibration.compute_house_edge_by_gpu_type`.
    *gpu_types_for_constraint* extracts the constraint's GPU pool — the
    planner already has this helper (``_gpu_types_in_constraint``);
    callers can pass it directly.

    For an alternation constraint (``"a40|a100"``) we apply the
    **worst-case** ratio across the pool members: if any member is
    systematically slower than predicted, the rank should reflect that
    — the scheduler can pick any pool member at runtime, and we'd
    rather over- than under-estimate the wait. Pool members with no
    calibration data are skipped (treated as "trust raw"), so a fully
    cold-start pool yields ``factor=None`` and the raw ETA passes
    through unchanged.

    The factor is clamped to ``[floor, ceiling]`` so a single freak
    sample (e.g., a 100× outlier from a clock-skew bug) cannot make the
    lattice rank nonsensical.
    """
    out: list[CalibratedProbe] = []
    for probe in probes:
        gpu_types = tuple(gpu_types_for_constraint(probe.tuple_.constraint))
        if probe.eta_sec is None:
            out.append(
                CalibratedProbe(
                    probe=probe,
                    eta_sec_calibrated=None,
                    factor=None,
                    gpu_types=gpu_types,
                )
            )
            continue

        ratios = [
            float(edges_by_gpu_type[g].calibration_ratio)
            for g in gpu_types
            if g in edges_by_gpu_type and edges_by_gpu_type[g].calibration_ratio is not None
        ]
        if not ratios:
            out.append(
                CalibratedProbe(
                    probe=probe,
                    eta_sec_calibrated=probe.eta_sec,
                    factor=None,
                    gpu_types=gpu_types,
                )
            )
            continue

        factor = max(floor, min(ceiling, max(ratios)))
        adjusted = int(round(probe.eta_sec * factor))
        out.append(
            CalibratedProbe(
                probe=probe,
                eta_sec_calibrated=adjusted,
                factor=factor,
                gpu_types=gpu_types,
            )
        )
    return out


def pick_earliest_calibrated(
    calibrated: list[CalibratedProbe],
) -> CalibratedProbe | None:
    """Return the calibrated probe with the smallest adjusted ETA.

    Mirrors :func:`pick_earliest`'s tie-break (prefer smaller walltime
    ask). Probes with ``eta_sec_calibrated is None`` are skipped — they
    have no usable signal to rank by.
    """
    eligible = [c for c in calibrated if isinstance(c.eta_sec_calibrated, int)]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda c: (c.eta_sec_calibrated, c.probe.tuple_.walltime_sec),
    )
