"""DES backend wiring for the queue-wait predictor.

Extracted from :mod:`queue_wait_baseline` (was Phase 4c additions
appended to that file). The diurnal-MA core stays there; this module
owns the auto-fallback eligibility check + DES integration so the
baseline file is closer to one concern (per-bucket weighted means)
instead of two.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent_pro.forecast.queue_wait_baseline import (
    _DEFAULT_BUCKET_RADIUS,
    _DEFAULT_HALF_LIFE_DAYS,
    _DEFAULT_MIN_BUCKET_SAMPLES,
    _DEFAULT_MIN_GLOBAL_SAMPLES,
    Method,
    PredictionResult,
    _hour_of_week,
    _predict_diurnal_ma,
)

if TYPE_CHECKING:
    from hpc_agent_pro.forecast.queue_features import QueueFeatures

# ---------------------------------------------------------------------------
# DES backend wiring (Phase 4c)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DESDecision:
    """Outcome of the auto-fallback eligibility check."""

    eligible: bool
    reason: str
    n_running_users: int
    n_known_users: int


# Auto-fallback rule: DES needs (a) at least one recent cluster snapshot
# AND (b) user profiles for >= 80% of currently-running jobs' users.
_DES_AUTO_USER_COVERAGE_THRESHOLD = 0.8


def _des_eligible(experiment_dir: Path, *, cluster: str) -> _DESDecision:
    """Decide whether the auto path should pick DES over diurnal_ma.

    Defensive: any import error or filesystem hiccup yields
    ``eligible=False`` with a populated reason — never raises.
    """
    try:
        from hpc_agent.infra.inspect import read_cluster_history

        from hpc_agent_pro.forecast.queue_simulator import extract_running_jobs
    except ImportError as exc:
        return _DESDecision(False, f"import failed: {exc}", 0, 0)

    snapshots = list(read_cluster_history(experiment_dir, cluster, limit=1))
    if not snapshots:
        return _DESDecision(False, "no cluster_history snapshots persisted", 0, 0)

    running = extract_running_jobs(snapshots[0])
    running_users = {j.user for j in running if j.user}
    if not running_users:
        # Empty cluster: DES is fine — no residuals to estimate, no
        # profile coverage to demand.
        return _DESDecision(True, "snapshot present, cluster idle", 0, 0)

    try:
        from hpc_agent.state.user_profiles import all_profiles
    except ImportError:
        return _DESDecision(False, "user_profiles module unavailable", len(running_users), 0)
    profiles = all_profiles(experiment_dir, cluster=cluster)
    known = sum(1 for u in running_users if u in profiles)
    coverage = known / len(running_users)
    if coverage >= _DES_AUTO_USER_COVERAGE_THRESHOLD:
        return _DESDecision(
            True,
            f"snapshot + {coverage:.0%} user-profile coverage",
            len(running_users),
            known,
        )
    return _DESDecision(
        False,
        f"only {coverage:.0%} user-profile coverage; need >=80%",
        len(running_users),
        known,
    )


def _default_candidate(profile: str) -> Any:
    """Build a tiny default SimJob when the caller didn't pass one.

    1 CPU, 4 GB, no GPU — useful for "how empty is the queue" probes; a
    real planner should always pass the actual submit shape.
    """
    from hpc_agent_pro.forecast.queue_simulator import SimJob

    return SimJob(
        job_id=f"candidate-{profile}",
        user="candidate",
        submit_time=0.0,
        walltime_ask=3600.0,
        cpus=1,
        mem_mb=4_000,
    )


def _profile_to_dict(p: Any) -> dict[str, Any]:
    """Convert a UserProfile dataclass to a plain dict.

    Defensive: accepts either a dict or a dataclass-like object.
    """
    if isinstance(p, dict):
        return p
    out: dict[str, Any] = {}
    for fld in (
        "user",
        "n_observations",
        "median_submits_per_day",
        "submit_hour_of_week_distribution",
        "median_walltime_ask_sec",
        "median_actual_over_ask",
        "median_array_size",
        "typical_gpu_types",
        "failure_rate",
        "p_followup_within_6h",
    ):
        if hasattr(p, fld):
            out[fld] = getattr(p, fld)
    return out


def _retag_method(result: PredictionResult, method: Method, reason: str) -> PredictionResult:
    """Replace ``method`` and prepend ``reason`` to ``fallback_reason``."""
    new_reason = reason if result.fallback_reason is None else f"{reason}; {result.fallback_reason}"
    return PredictionResult(
        predicted_wait_sec=result.predicted_wait_sec,
        confidence=result.confidence,
        method=method,
        n_bucket_samples=result.n_bucket_samples,
        n_total_samples=result.n_total_samples,
        bucket_hour_of_week=result.bucket_hour_of_week,
        fallback_reason=new_reason,
        features_adjustment_factor=result.features_adjustment_factor,
        p10_wait_sec=result.p10_wait_sec,
        p90_wait_sec=result.p90_wait_sec,
        n_replications=result.n_replications,
    )


def _predict_des(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    at_iso: str | None,
    n_replications: int,
    candidate: Any | None,
    seed: int | None,
    current_features: QueueFeatures | None,
) -> PredictionResult:
    """DES backend: simulate the scheduler forward against the latest snapshot.

    Falls back to ``diurnal_ma`` (with method tagged ``des_no_snapshot``)
    when the prerequisites are missing — that way the caller still gets
    a number rather than a hard error.
    """
    from hpc_agent.infra.inspect import read_cluster_history

    from hpc_agent_pro.forecast.queue_simulator import (
        SimJob,
        extract_running_jobs,
        simulate_distribution,
    )
    from hpc_agent_pro.forecast.queue_simulator_inputs import (
        sample_arrival_stream,
        sample_residual_lifetimes,
    )

    snapshots = list(read_cluster_history(experiment_dir, cluster, limit=1))
    if not snapshots:
        fallback = _predict_diurnal_ma(
            experiment_dir,
            profile=profile,
            cluster=cluster,
            at_iso=at_iso,
            half_life_days=_DEFAULT_HALF_LIFE_DAYS,
            min_bucket_samples=_DEFAULT_MIN_BUCKET_SAMPLES,
            min_global_samples=_DEFAULT_MIN_GLOBAL_SAMPLES,
            bucket_radius=_DEFAULT_BUCKET_RADIUS,
            current_features=current_features,
        )
        return _retag_method(
            fallback,
            "des_no_snapshot",
            "DES requested but no cluster_history snapshot persisted",
        )
    snap = snapshots[0]

    try:
        from hpc_agent.state.user_profiles import all_profiles

        profiles = all_profiles(experiment_dir, cluster=cluster)
    except ImportError:
        profiles = {}

    cand = candidate if isinstance(candidate, SimJob) else _default_candidate(profile)
    # ``hour_of_week`` legitimately returns 0 (Monday 00:00 UTC); using
    # ``or 0`` would collapse a None ("unparseable") to the same value as
    # a valid Monday-midnight bucket. Branch explicitly.
    _snap_how_raw = _hour_of_week(snap.now_iso)
    snap_how = 0 if _snap_how_raw is None else _snap_how_raw
    profile_dicts = {
        u: (p if isinstance(p, dict) else _profile_to_dict(p)) for u, p in profiles.items()
    }

    def _arr_sampler(s: int) -> list[SimJob]:
        return sample_arrival_stream(
            profile_dicts,
            snap_hour_of_week=snap_how,
            horizon_sec=7 * 86400.0,
            seed=s,
        )

    def _res_sampler(s: int) -> dict[str, float]:
        return sample_residual_lifetimes(snap, profile_dicts, seed=s)

    sim_out = simulate_distribution(
        snap,
        candidate=cand,
        n_replications=n_replications,
        seed=seed,
        arrival_sampler=_arr_sampler,
        residual_sampler=_res_sampler,
    )

    n_running = len(extract_running_jobs(snap))
    # ``hour_of_week`` returns ``0`` for Monday midnight; ``or -1`` would
    # mis-tag that valid bucket as "unparseable". Branch on None.
    _target_raw = _hour_of_week(at_iso or snap.now_iso)
    target_bucket = -1 if _target_raw is None else _target_raw
    base = PredictionResult(
        predicted_wait_sec=int(round(sim_out.p50_wait_sec)),
        confidence="medium" if n_running > 0 else "low",
        method="des",
        n_bucket_samples=n_running,
        n_total_samples=len(profiles),
        bucket_hour_of_week=target_bucket,
        fallback_reason=None,
        features_adjustment_factor=1.0,
        p10_wait_sec=int(round(sim_out.p10_wait_sec)),
        p90_wait_sec=int(round(sim_out.p90_wait_sec)),
        n_replications=n_replications,
    )
    return base
