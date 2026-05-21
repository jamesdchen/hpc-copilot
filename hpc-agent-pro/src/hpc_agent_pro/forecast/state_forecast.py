"""Forecast available cluster resources at a future offset T.

Phase 2c: combine a current :class:`ClusterSnapshot` with per-user
:class:`UserProfile`s to project how much of the cluster will be free
``t_offset_sec`` seconds from now.

Method
------
For each currently-running job in ``snap.co_tenants``:

1. Look up the job's user in *profiles* (default: cold-start ratio).
2. Predict the residual lifetime via
   :func:`hpc_agent_pro.forecast.residual_lifetime.predict_residual_lifetime`.
3. If the residual is ``<= t_offset_sec``, the job will have completed
   by T — sum its CPU / memory / GPU contribution into the
   "expected completed" pool.

The forecast assumes scheduler turnover during the window keeps
allocation pressure roughly constant; it doesn't try to predict new
arrivals (those go through the diurnal MA in
:mod:`hpc_agent_pro.forecast.queue_wait_baseline`). The output is meant
as an additional regression signal alongside the diurnal MA, not a
standalone wait predictor.

Edge cases
----------
- ``snap.co_tenants`` empty → forecast equals current capacity.
- All jobs expected to complete by T → max available pool.
- Jobs that lack ``elapsed_s`` or ``walltime_ask_sec`` → assume
  they'll continue running (conservative).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent.state.user_profiles import UserProfile

from hpc_agent_pro.forecast.residual_lifetime import predict_residual_lifetime

if TYPE_CHECKING:
    from hpc_agent.infra.inspect import ClusterSnapshot

__all__ = ["ForecastedState", "forecast_state_at"]


@dataclass(frozen=True)
class ForecastedState:
    """Projected free-resource pool ``t_offset_sec`` from now.

    All fields aggregate across the cluster. ``available_*`` reflects
    capacity that *will be free* at the offset, computed as
    ``total - (current_alloc - completing_by_T)``.
    """

    t_offset_sec: int
    available_gpus: int
    available_cpus: int
    available_mem_gb: float
    n_jobs_completing_by_t: int


def _gpu_count_from_gres(gres: str) -> int:
    """Sum GPU counts from a SLURM-style GRES string.

    Accepts ``gpu:a100:2``, ``gpu:2``, multiple comma-separated entries.
    Permissive: 0 on parse failure.
    """
    if not gres:
        return 0
    total = 0
    for raw in gres.split(","):
        raw = raw.strip()
        if not raw.startswith("gpu"):
            continue
        # Strip trailing index list "(IDX:0-3)".
        bare = raw.split("(", 1)[0]
        parts = bare.split(":")
        # gpu:N or gpu:type:N
        try:
            if len(parts) == 2:
                total += int(parts[1])
            elif len(parts) >= 3:
                total += int(parts[2])
        except ValueError:
            continue
    return total


def _to_int_zero(x: Any) -> int:
    try:
        return int(x) if x is not None else 0
    except (TypeError, ValueError):
        return 0


def _to_float_zero(x: Any) -> float:
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def forecast_state_at(
    snap: ClusterSnapshot,
    *,
    t_offset_sec: int,
    profiles: dict[str, UserProfile] | None = None,
    fallback_ratio: float = 0.85,
) -> ForecastedState:
    """Forecast the cluster's free-resource pool *t_offset_sec* from now.

    Parameters
    ----------
    snap:
        Current cluster snapshot.
    t_offset_sec:
        Lookahead horizon in seconds. ``0`` returns the current free
        pool; large values approach the cluster's total capacity as
        running jobs complete.
    profiles:
        Optional ``{user: UserProfile}`` map. Users absent from the
        map (or callers passing ``None``) get the *fallback_ratio*
        for their residual-lifetime prediction.
    fallback_ratio:
        Forwarded to :func:`predict_residual_lifetime` for users
        without a profile or with too few observations.
    """
    if profiles is None:
        profiles = {}

    total_gpus = 0
    total_cpus = 0
    total_mem_mb = 0
    alloc_gpus = 0
    alloc_cpus = 0
    alloc_mem_gb = 0.0
    completing_gpus = 0
    completing_cpus = 0
    completing_mem_gb = 0.0
    n_completing = 0

    # All upper-case so the ``state.upper() in pending`` check below works
    # for SGE rows that report "qw" verbatim — the v1 fix to
    # ``queue_features._PENDING_STATES`` corrected this for the order-book
    # feature path but the sibling state-forecaster was missed and
    # SGE-cluster pendings were silently classified as RUNNING.
    pending = {"PD", "PENDING", "QUEUED", "QW"}

    for node in snap.nodes:
        if node.is_drained:
            continue
        # Capacity totals.
        total_gpus += _gpu_count_from_gres(node.gres)
        if node.cpu_tot is not None:
            total_cpus += int(node.cpu_tot)
        if node.real_mem_mb is not None:
            total_mem_mb += int(node.real_mem_mb)

        # Currently-allocated resources (sum of running co-tenants).
        for tenant in node.co_tenants:
            state = str(tenant.get("state") or "")
            if state and state.upper() in pending:
                continue
            cpus = _to_int_zero(tenant.get("cpus"))
            mem_gb = _to_float_zero(tenant.get("mem_gb"))
            gpus = _to_int_zero(tenant.get("gpus"))
            alloc_cpus += cpus
            alloc_mem_gb += mem_gb
            alloc_gpus += gpus

            # Predict whether this job completes inside the window.
            elapsed = _to_int_zero(tenant.get("elapsed_s"))
            ask = _to_int_zero(tenant.get("walltime_ask_sec"))
            if ask <= 0 or elapsed < 0:
                continue
            user = str(tenant.get("user") or "")
            profile = profiles.get(user) if user else None
            if profile is None:
                # Synthesize a thin profile so the estimator falls
                # through to fallback_ratio.
                profile = UserProfile(user=user, n_observations=0)
            residual = predict_residual_lifetime(
                profile=profile,
                elapsed_sec=elapsed,
                walltime_ask_sec=ask,
                fallback_ratio=fallback_ratio,
            )
            if residual <= max(0, int(t_offset_sec)):
                completing_gpus += gpus
                completing_cpus += cpus
                completing_mem_gb += mem_gb
                n_completing += 1

    # Available at T = total - (alloc - completing_by_T).
    avail_gpus = max(0, total_gpus - max(0, alloc_gpus - completing_gpus))
    avail_cpus = max(0, total_cpus - max(0, alloc_cpus - completing_cpus))
    avail_mem_gb = max(
        0.0,
        (total_mem_mb / 1024.0) - max(0.0, alloc_mem_gb - completing_mem_gb),
    )

    return ForecastedState(
        t_offset_sec=int(t_offset_sec),
        available_gpus=int(avail_gpus),
        available_cpus=int(avail_cpus),
        available_mem_gb=round(avail_mem_gb, 2),
        n_jobs_completing_by_t=int(n_completing),
    )
