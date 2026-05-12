"""Sampling helpers for the queue-wait DES.

The DES's variance comes from two stochastic inputs:

1. **Future arrivals.** Per-user behavioural priors (Phase 2a) yield a
   non-homogeneous Poisson rate (median submits per day × hour-of-week
   diurnal factor). Sampling a Poisson process over the prediction
   horizon gives one realization of "what other users do next."
2. **Residual lifetimes of running jobs.** Each running job's actual
   runtime is the user's empirical actual-over-ask ratio applied to the
   walltime ask, with multiplicative noise. Sampling many realizations
   gives the simulator a distribution to fan over.

Phase 2a's :class:`UserProfile` is treated as duck-typed (both dict
and dataclass shapes accepted, plus alternate field names — the spec
evolved during implementation). Recognised field aliases::

    median_submits_per_day             — Poisson base rate
    submit_hour_of_week_distribution   — dict[int, float] mapping
        0..167 -> normalized weight; OR
    hour_of_week_factors               — list[float] length 168
    median_walltime_ask_sec            — common walltime ask
    median_actual_over_ask             — runtime ratio center
    actual_over_ask_p10/p90            — optional ratio quantiles
    typical_gpu_types                  — list[str]; first item used
    common_cpus / common_mem_mb / common_gpus
                                       — job-shape fields used when
        Phase 2a hasn't yet inferred them; defaults are 1 cpu, 4 GB,
        0 gpus.

Any missing field falls through to a sensible default — the planner
must not require fully populated profiles to get a usable forecast.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING, Any

from claude_hpc.forecast.queue_simulator import SimJob, extract_running_jobs

if TYPE_CHECKING:
    from claude_hpc.infra.inspect import ClusterSnapshot

__all__ = [
    "sample_arrival_stream",
    "sample_residual_lifetimes",
]


_DEFAULT_HOW_FACTORS_FLAT = [1.0] * 168  # uniform diurnal — used when missing


def _profile_get(profile: Any, key: str, default: Any) -> Any:
    """Defensive accessor — accepts dict or dataclass-like."""
    if profile is None:
        return default
    if isinstance(profile, dict):
        return profile.get(key, default)
    return getattr(profile, key, default)


def _resolve_how_factors(profile: Any) -> list[float]:
    """Resolve diurnal multipliers from either schema variant.

    Phase 2a's UserProfile carries ``submit_hour_of_week_distribution``
    as ``dict[int, float]`` (already normalized). The earlier draft used
    ``hour_of_week_factors: list[float]`` of length 168. Either is
    accepted; missing falls back to flat.
    """
    raw = _profile_get(profile, "hour_of_week_factors", None)
    if raw is None:
        dist = _profile_get(profile, "submit_hour_of_week_distribution", None)
        if isinstance(dist, dict) and dist:
            factors = [0.0] * 168
            for k, v in dist.items():
                try:
                    idx = int(k) % 168
                except (TypeError, ValueError):
                    continue
                factors[idx] = float(v)
            # Distribution sums to ~1.0; convert to per-hour multiplier
            # (mean 1.0 over 168 buckets).
            total = sum(factors)
            if total > 0:
                factors = [f * 168.0 / total for f in factors]
            return factors
        return list(_DEFAULT_HOW_FACTORS_FLAT)
    factors = list(raw)
    if len(factors) != 168 or sum(factors) <= 0:
        return list(_DEFAULT_HOW_FACTORS_FLAT)
    return factors


def _resolve_walltime_ask(profile: Any) -> float:
    """Common walltime ask — accept either field name."""
    v = _profile_get(profile, "common_walltime_ask_sec", None)
    if v is not None:
        return float(v)
    v = _profile_get(profile, "median_walltime_ask_sec", None)
    if v is not None and v > 0:
        return float(v)
    return 3600.0


def _resolve_gpu_type(profile: Any) -> str:
    """Resolve GPU type — accept ``common_gpu_type`` or ``typical_gpu_types[0]``."""
    v = _profile_get(profile, "common_gpu_type", None)
    if v:
        return str(v)
    types = _profile_get(profile, "typical_gpu_types", None)
    if isinstance(types, (list, tuple)) and types:
        return str(types[0])
    return ""


def _hour_of_week_at(t_sec: float, snap_now_sec: float, snap_hour_of_week: int) -> int:
    """Project ``t_sec`` (offset from sim start) to a 0..167 bucket.

    ``snap_hour_of_week`` is the hour-of-week of the snapshot's
    ``now_iso``. We add the elapsed hours and wrap mod 168.
    """
    elapsed_hours = int((t_sec - snap_now_sec) // 3600)
    return (snap_hour_of_week + elapsed_hours) % 168


def sample_arrival_stream(
    user_profiles: dict[str, Any],
    *,
    snap_hour_of_week: int = 0,
    horizon_sec: float = 7 * 86400.0,
    seed: int | None = None,
    job_id_prefix: str = "arr",
) -> list[SimJob]:
    """Sample future submissions per per-user non-homogeneous Poisson.

    For each user in ``user_profiles``:

    * compute the base instantaneous rate
      ``λ = median_submits_per_day / 86400`` (Hz).
    * apply the diurnal multiplier ``hour_of_week_factors[h]`` (1.0 mean,
      so multiplying preserves the user's daily total).
    * sample inter-arrival times via the thinning algorithm: at each
      step pick the candidate ``Δ ~ Exp(λ_max)``, accept with
      probability ``λ(t)/λ_max``.

    Each accepted arrival becomes a ``SimJob`` whose shape is drawn from
    the user's ``common_*`` fields.

    Returns a flat list of ``SimJob`` sorted by ``submit_time``. Empty
    list when ``user_profiles`` is empty or contains no usable rates.
    """
    if not user_profiles:
        return []
    rng = random.Random(seed)
    arrivals: list[SimJob] = []
    counter = 0
    for uname, profile in user_profiles.items():
        rate_per_day = float(_profile_get(profile, "median_submits_per_day", 0.0))
        if rate_per_day <= 0:
            continue
        factors = _resolve_how_factors(profile)
        # Normalise factors to mean 1.0 so the daily-total invariant
        # holds regardless of upstream conventions.
        mean_f = sum(factors) / 168.0
        if mean_f > 0 and abs(mean_f - 1.0) > 1e-3:
            factors = [f / mean_f for f in factors]
        lam_max = (rate_per_day / 86400.0) * max(factors)
        if lam_max <= 0:
            continue
        t = 0.0
        while t < horizon_sec:
            # Exponential candidate. ``rng.random()`` returns in [0, 1);
            # ``1.0 - r`` then lives in (0, 1] so ``log(u)`` is finite
            # and we don't need a guard against u == 0.
            u = 1.0 - rng.random()
            t += -math.log(u) / lam_max
            if t >= horizon_sec:
                break
            how = _hour_of_week_at(t, 0.0, snap_hour_of_week)
            lam_t = (rate_per_day / 86400.0) * factors[how]
            if rng.random() > lam_t / lam_max:
                continue
            counter += 1
            arrivals.append(
                SimJob(
                    job_id=f"{job_id_prefix}-{uname}-{counter}",
                    user=str(uname),
                    submit_time=t,
                    walltime_ask=float(_resolve_walltime_ask(profile)),
                    cpus=int(_profile_get(profile, "common_cpus", 1)),
                    mem_mb=int(_profile_get(profile, "common_mem_mb", 4_000)),
                    gpus=int(_profile_get(profile, "common_gpus", 0)),
                    gpu_type=_resolve_gpu_type(profile),
                )
            )
    arrivals.sort(key=lambda j: j.submit_time)
    return arrivals


def sample_residual_lifetimes(
    snapshot: ClusterSnapshot,
    user_profiles: dict[str, Any] | None = None,
    *,
    seed: int | None = None,
) -> dict[str, float]:
    """For each running job in ``snapshot`` sample its remaining seconds.

    Algorithm per job:

    * elapsed_sec = co_tenant.elapsed_s
    * walltime_ask = user's ``median_walltime_ask_sec`` /
      ``common_walltime_ask_sec`` from the profile when available; falls
      back to ``elapsed + 1h`` for unknown users. The previous form
      always used the ``elapsed + 1h`` heuristic, which biased long-
      running jobs to a residual near zero — a 24h job got a residual
      <=1h with high probability, distorting the DES sim's view of when
      slots free up.
    * Sample ratio = ``Triangular(p10, median, p90)`` over the user's
      ``actual_over_ask`` distribution; default to ``Triangular(0.6, 0.9, 1.0)``
      when the profile is missing.
    * actual_runtime_total = walltime_ask × ratio
    * residual = max(0, actual_runtime_total - elapsed_sec)

    Returns ``{job_id: residual_sec_offset_from_sim_start}``. Jobs whose
    user is unknown to ``user_profiles`` use the default ratio. The
    returned values are ABSOLUTE end times measured from the sim's t=0
    (since running jobs all started before t=0, residual == end_time).
    """
    rng = random.Random(seed)
    profiles = user_profiles or {}
    out: dict[str, float] = {}
    for j in extract_running_jobs(snapshot):
        prof = profiles.get(j.user) if isinstance(profiles, dict) else None
        p10 = float(_profile_get(prof, "actual_over_ask_p10", 0.6))
        med = float(_profile_get(prof, "median_actual_over_ask", 0.9))
        p90 = float(_profile_get(prof, "actual_over_ask_p90", 1.0))
        # Order-clip in case the profile rows are inconsistent.
        lo = min(p10, med, p90)
        hi = max(p10, med, p90)
        mode = max(lo, min(hi, med))
        ratio = rng.triangular(lo, hi, mode)
        elapsed = -j.submit_time
        # Replace the snapshot-derived ``elapsed + 3600`` heuristic with
        # the user's typical walltime ask when available. ``j.walltime_ask``
        # is still the heuristic (extract_running_jobs doesn't see the
        # profile), so override it here.
        profile_ask = _profile_get(prof, "median_walltime_ask_sec", None)
        if profile_ask is None:
            profile_ask = _profile_get(prof, "common_walltime_ask_sec", None)
        if profile_ask is not None and float(profile_ask) > 0:
            walltime_ask = float(profile_ask)
        else:
            walltime_ask = j.walltime_ask  # heuristic fallback
        walltime_total = walltime_ask * ratio
        residual = max(0.0, walltime_total - elapsed)
        out[j.job_id] = residual
    return out
