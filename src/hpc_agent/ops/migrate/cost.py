"""Canary-calibrated cost estimate for a remainder migration [SPEC §3 Step C, Δ7].

The cost third of ``migrate-remainder``: ``undone_count × calibrated_per_task_
walltime × effective_cores / 3600``. Two things make this DIFFERENT from
``retarget-run``'s ``_cost_estimate`` (which the SPEC's Δ7 finding names): retarget
estimates the **full grid** at a **cold** target walltime, whereas a migration
estimates only the **undone count** at the **source-observed** per-task runtime.

The per-task walltime basis is the source canary's stamped measured wall-clock
(``read_canary_elapsed_sec`` on the ``<source_run_id>-canary`` sibling,
``state/runs.py:1150``), right-sized shrink-only against a ceiling via
``calibrate_array_walltime`` (``ops/submit/canary_calibration.py:110``). Fed to the
ONE footprint→core-hours kernel ``estimate_core_hours`` (``infra/cost.py:125``); its
``footprint_unknown`` honesty (``cost.py:88``) is what the brief renders as "unknown
core-hours" instead of a false "0" (proving run #6).

**Cross-cluster honesty [SPEC §3.C].** The prior is **cluster-agnostic core-hours**
— there is no per-cluster speed / scaling field in ``clusters.yaml``
(``infra/clusters.py`` carries ceilings / defaults only), so the estimate is
disclosed as "N core-hours from source-observed runtime, portable to <target> as
core-hours; the target's own history is cold-start (needs_canary) so the S2 canary
re-calibrates." This module actuates nothing — pure arithmetic over already-read
sidecar values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent.infra.cost import CostEstimate, estimate_core_hours
from hpc_agent.ops.submit.canary_calibration import calibrate_array_walltime
from hpc_agent.state.runs import read_canary_elapsed_sec

__all__ = ["MigrationCostEstimate", "estimate_migration_cost"]


@dataclass(frozen=True)
class MigrationCostEstimate:
    """The undone-count × source-observed-runtime footprint for the target.

    ``core_hours`` is the underlying :class:`~hpc_agent.infra.cost.CostEstimate`
    (its ``footprint_unknown`` drives the "unknown core-hours" rendering). The
    remaining fields are the disclosed basis: whether the per-task walltime came
    from a real source canary measurement (``calibrated_from_canary``), the walltime
    it resolved to, and the cluster-agnostic-core-hours disclosure string.
    """

    undone_count: int
    per_task_walltime_sec: int
    cores_per_task: int
    calibrated_from_canary: bool
    core_hours: CostEstimate
    disclosure: str

    @property
    def est_core_hours(self) -> float:
        """The estimated core-hours (0.0 when the footprint is unknown)."""
        return self.core_hours.est_core_hours

    @property
    def footprint_unknown(self) -> bool:
        """True when nothing was measured — the brief must say "unknown", not "0"."""
        return self.core_hours.footprint_unknown

    def to_brief(self) -> dict[str, Any]:
        """The nested ``cost_estimate`` block the migration brief carries."""
        return {
            "undone_count": self.undone_count,
            "per_task_walltime_sec": self.per_task_walltime_sec,
            "cores_per_task": self.cores_per_task,
            "calibrated_from_canary": self.calibrated_from_canary,
            "est_core_hours": self.core_hours.est_core_hours,
            "footprint_unknown": self.core_hours.footprint_unknown,
            "disclosure": self.disclosure,
        }


def _resources_walltime_ceiling(source_resources: dict[str, Any]) -> int | None:
    """The source's requested per-task walltime ceiling (seconds), or None.

    Reads ``walltime_sec`` (the canonical key) then ``walltime`` (the legacy alias
    ``revise-resolved`` also round-trips). A non-positive / non-int value → None so
    the shrink-only calibration is a no-op rather than fabricating a ceiling.
    """
    for key in ("walltime_sec", "walltime"):
        raw = source_resources.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw > 0:
            return raw
    return None


def _resources_cores(source_resources: dict[str, Any]) -> int | None:
    """The source's per-task cpu request (effective cores), or None (kernel floors to 1)."""
    raw = source_resources.get("cpus")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def estimate_migration_cost(
    experiment_dir: Path,
    *,
    source_run_id: str,
    undone_count: int,
    source_resources: dict[str, Any],
    target_cluster: str,
) -> MigrationCostEstimate:
    """Estimate the migration's footprint over *undone_count* on *target_cluster*.

    The per-task walltime is resolved in this order (SPEC §3.C):

    1. the source canary's stamped measured wall-clock, shrink-only right-sized
       against the source's requested ceiling (``calibrate_array_walltime``) — the
       honest source-observed basis;
    2. the raw canary measurement when there is no requested ceiling to shrink
       against (calibration is a no-op, but a real measurement is still the best
       basis — never discard it for the ceiling's absence);
    3. the requested ceiling when there is no canary measurement (a cold estimate);
    4. otherwise 0 → the underlying :func:`estimate_core_hours` returns its
       defensive zero and ``footprint_unknown`` is True, so the brief says "unknown
       core-hours" (proving run #6), never a false "0".

    Actuates nothing — reads the source's already-persisted canary sidecar + the
    resources dict passed in.
    """
    # The source's OWN canary sibling carries the observed per-task runtime.
    canary_elapsed = read_canary_elapsed_sec(experiment_dir, f"{source_run_id}-canary")
    ceiling = _resources_walltime_ceiling(source_resources)
    cores = _resources_cores(source_resources)

    cal = calibrate_array_walltime(
        canary_elapsed_sec=canary_elapsed,
        requested_walltime_sec=ceiling,
    )

    calibrated_from_canary = False
    if cal.applied and cal.walltime_sec:
        # Shrink-only source-observed basis (canary measured AND a ceiling to fit under).
        walltime = int(cal.walltime_sec)
        calibrated_from_canary = True
    elif canary_elapsed and canary_elapsed > 0:
        # A real measurement with no ceiling to shrink against — still the best basis.
        walltime = int(canary_elapsed)
        calibrated_from_canary = True
    elif ceiling:
        # No measurement: fall back to the requested ceiling (a cold estimate).
        walltime = int(ceiling)
    else:
        # Nothing measured, nothing requested — footprint is UNKNOWN, not free.
        walltime = 0

    core_hours = estimate_core_hours(
        total_tasks=undone_count,
        walltime_s=walltime,
        cores_per_task=cores,
    )

    if core_hours.footprint_unknown:
        disclosure = (
            "unknown core-hours: no source canary measurement and no requested "
            "walltime to estimate from — the S2 canary on "
            f"{target_cluster!r} will measure the real per-task runtime (needs_canary)."
        )
    else:
        basis = (
            "source-observed canary runtime"
            if calibrated_from_canary
            else "the source's requested walltime ceiling (no canary measurement)"
        )
        disclosure = (
            f"{core_hours.est_core_hours:g} core-hours over {undone_count} undone "
            f"cells, from {basis}. The prior is cluster-AGNOSTIC core-hours (no "
            "per-cluster speed field in clusters.yaml); portable to "
            f"{target_cluster!r} as core-hours, but its own history is cold-start — "
            "the S2 canary re-calibrates on the target."
        )

    return MigrationCostEstimate(
        undone_count=undone_count,
        per_task_walltime_sec=walltime,
        cores_per_task=core_hours.cores_per_task,
        calibrated_from_canary=calibrated_from_canary,
        core_hours=core_hours,
        disclosure=disclosure,
    )
