"""Per-user behavioral priors derived from squeue + sacct snapshots.

Phase 2a of the queue-wait predictor plan: build a profile per user
from observed jobs over time so the predictor can condition forecasts
on "user X usually overshoots their walltime by 40%" or "user Y
submits a fresh array every Tuesday morning".

Storage layout
--------------
One file per cluster, dict keyed by username::

    <experiment_dir>/.hpc/user_profiles/<cluster>.json

Atomic write through :func:`claude_hpc._internal._io.atomic_locked_update` so
concurrent updates from multiple agent sessions are serialised.

Profile fields
--------------
:class:`UserProfile` aggregates submission cadence, walltime
ask-vs-actual ratios, job shape, reliability, and a rough
follow-up-job conditional probability. All fields tolerate sparse
input — a user with two observations gets a thin profile, but a
profile nonetheless. Callers (e.g. the residual-lifetime estimator
in :mod:`claude_hpc.forecast.residual_lifetime`) gate on
``n_observations >= threshold`` before trusting the medians.

The rolling aggregator does NOT store raw observations — that would
explode the on-disk footprint. Instead it keeps a *running* median
estimator (exponential blend with a configurable smoothing factor)
and frequency tables for histogram-shaped fields. Trade-off: median
converges asymptotically rather than tracking the true sample
median, but for an advisory prior that's a reasonable price.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from claude_hpc._internal._io import atomic_locked_update
from claude_hpc._internal._time import parse_iso_utc_or_none

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = [
    "UserProfile",
    "update_profile",
    "read_profile",
    "all_profiles",
    "user_profiles_path",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1

# Smoothing factor for the running-median estimator. 0.05 means each
# new observation moves the estimator ~5% of the way toward it. With
# 60+ observations the estimate is within a few percent of the true
# median for stationary distributions.
_MEDIAN_SMOOTHING = 0.05


@dataclass(frozen=True)
class UserProfile:
    """Per-user behavioral profile.

    All fields are advisory. Callers should treat ``n_observations <
    threshold`` profiles as cold-start and fall back to per-cluster
    averages.
    """

    user: str
    n_observations: int
    # Submission rate
    median_submits_per_day: float = 0.0
    submit_hour_of_week_distribution: dict[int, float] = field(default_factory=dict)
    # Walltime ask vs actual
    median_walltime_ask_sec: int = 0
    # Ratio actual_elapsed / walltime_ask. 0.6 means "always overshoots
    # ask by 40%"; 1.0 means "always uses the ask exactly".
    median_actual_over_ask: float = 1.0
    # Job shape
    median_array_size: int = 1
    typical_gpu_types: list[str] = field(default_factory=list)
    # Reliability
    failure_rate: float = 0.0  # frac of jobs exit_code != 0
    # Dependency chains
    p_followup_within_6h: float = 0.0


def user_profiles_path(experiment_dir: Path, cluster: str) -> Path:
    """Return the per-cluster user-profiles JSON path.

    Forwarder for ``RepoLayout(experiment_dir).hpc /
    "user_profiles/<cluster>.json"`` so callers don't reach into the
    layout directly.
    """
    if not cluster:
        raise ValueError("cluster must be non-empty")
    from claude_hpc._internal.layout import RepoLayout

    safe_cluster = cluster.replace("/", "_")
    base = RepoLayout(experiment_dir).hpc / "user_profiles"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{safe_cluster}.json"


def _empty_doc(cluster: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "cluster": cluster,
        "users": {},
    }


def _running_blend(prev: float, new_value: float, weight: float) -> float:
    """Exponential blend of a running estimate toward a new observation.

    ``prev`` of 0 with a fresh observation snaps to ``new_value``
    rather than blending toward zero — this lets the estimator
    converge quickly when a user has no prior history.
    """
    if prev == 0.0:
        return new_value
    return (1.0 - weight) * prev + weight * new_value


def _hour_of_week(iso: str | None) -> int | None:
    dt = parse_iso_utc_or_none(iso)
    if dt is None:
        return None
    return dt.weekday() * 24 + dt.hour


def _normalize_hist(hist: dict[int, float]) -> dict[int, float]:
    """Normalise a sparse hour-of-week histogram so values sum to 1."""
    total = sum(hist.values())
    if total <= 0:
        return {}
    return {k: round(v / total, 6) for k, v in hist.items()}


def _coerce_user_dict(d: Any, user: str) -> dict[str, Any]:
    """Return a well-shaped per-user record, defaulting fields."""
    if not isinstance(d, dict):
        d = {}
    d.setdefault("user", user)
    d.setdefault("n_observations", 0)
    d.setdefault("median_submits_per_day", 0.0)
    d.setdefault("submit_hour_of_week_counts", {})
    d.setdefault("median_walltime_ask_sec", 0)
    d.setdefault("median_actual_over_ask", 1.0)
    d.setdefault("median_array_size", 1)
    d.setdefault("typical_gpu_types", {})  # gpu_type -> count
    d.setdefault("failure_rate", 0.0)
    d.setdefault("p_followup_within_6h", 0.0)
    d.setdefault("last_seen_iso", None)
    return d


def _to_profile(record: dict[str, Any]) -> UserProfile:
    """Convert the on-disk dict shape to a public UserProfile."""
    counts: dict[str, int] = record.get("submit_hour_of_week_counts") or {}
    int_counts: dict[int, float] = {}
    for k, v in counts.items():
        try:
            int_counts[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    gpu_counts: dict[str, int] = record.get("typical_gpu_types") or {}
    typical = sorted(gpu_counts, key=lambda g: -int(gpu_counts.get(g, 0)))
    # Cap to top 4 — the predictor only consults the dominant types.
    typical = typical[:4]
    return UserProfile(
        user=record.get("user", ""),
        n_observations=int(record.get("n_observations", 0)),
        median_submits_per_day=float(record.get("median_submits_per_day", 0.0)),
        submit_hour_of_week_distribution=_normalize_hist(int_counts),
        median_walltime_ask_sec=int(record.get("median_walltime_ask_sec", 0)),
        median_actual_over_ask=float(record.get("median_actual_over_ask", 1.0)),
        median_array_size=int(record.get("median_array_size", 1)),
        typical_gpu_types=typical,
        failure_rate=float(record.get("failure_rate", 0.0)),
        p_followup_within_6h=float(record.get("p_followup_within_6h", 0.0)),
    )


def _fold_observation(record: dict[str, Any], obs: dict[str, Any]) -> dict[str, Any]:
    """Mutate *record* in place to absorb a single observation.

    The record is the on-disk per-user dict; *obs* mirrors the
    co-tenant rows produced by ``inspect_cluster`` plus optional
    ``submitted_at_iso`` / ``walltime_ask_sec`` / ``exit_code`` /
    ``followed_up_within_6h`` keys when known.
    """
    record["n_observations"] = int(record.get("n_observations", 0)) + 1
    n = record["n_observations"]

    sub_iso = obs.get("submitted_at_iso") or obs.get("submit_iso")
    bucket = _hour_of_week(sub_iso)
    if bucket is not None:
        counts = record.setdefault("submit_hour_of_week_counts", {})
        # JSON keys are strings on disk; keep them as strings here too
        # so atomic_locked_update's JSON round-trip is stable.
        key = str(bucket)
        counts[key] = float(counts.get(key, 0)) + 1.0

    walltime_ask = obs.get("walltime_ask_sec") or obs.get("walltime_requested_sec")
    if walltime_ask is not None:
        with contextlib.suppress(TypeError, ValueError):
            record["median_walltime_ask_sec"] = int(
                _running_blend(
                    float(record.get("median_walltime_ask_sec", 0)),
                    float(walltime_ask),
                    _MEDIAN_SMOOTHING,
                )
            )

    elapsed_sec = obs.get("elapsed_sec") or obs.get("elapsed_s")
    if elapsed_sec is not None and walltime_ask:
        try:
            ratio = float(elapsed_sec) / max(1.0, float(walltime_ask))
        except (TypeError, ValueError):
            ratio = None
        if ratio is not None and 0.0 < ratio < 10.0:
            record["median_actual_over_ask"] = round(
                _running_blend(
                    float(record.get("median_actual_over_ask", 1.0)),
                    ratio,
                    _MEDIAN_SMOOTHING,
                ),
                4,
            )

    array_size = obs.get("array_size")
    if array_size is not None:
        with contextlib.suppress(TypeError, ValueError):
            record["median_array_size"] = max(
                1,
                int(
                    _running_blend(
                        float(record.get("median_array_size", 1)),
                        float(array_size),
                        _MEDIAN_SMOOTHING,
                    )
                ),
            )

    gpu_type = obs.get("gpu_type")
    if gpu_type:
        gpus = record.setdefault("typical_gpu_types", {})
        gpus[gpu_type] = int(gpus.get(gpu_type, 0)) + 1

    exit_code = obs.get("exit_code")
    if exit_code is not None:
        try:
            failed = 1.0 if int(exit_code) != 0 else 0.0
        except (TypeError, ValueError):
            failed = None
        if failed is not None:
            # Online mean: prev = (n-1) successes/N, new contributes failed/N.
            prev = float(record.get("failure_rate", 0.0))
            record["failure_rate"] = round(((n - 1) * prev + failed) / n, 4)

    followup = obs.get("followed_up_within_6h")
    if isinstance(followup, bool):
        prev = float(record.get("p_followup_within_6h", 0.0))
        record["p_followup_within_6h"] = round(
            ((n - 1) * prev + (1.0 if followup else 0.0)) / n,
            4,
        )

    submits_per_day = obs.get("submits_per_day_window")
    if submits_per_day is not None:
        with contextlib.suppress(TypeError, ValueError):
            record["median_submits_per_day"] = round(
                _running_blend(
                    float(record.get("median_submits_per_day", 0.0)),
                    float(submits_per_day),
                    _MEDIAN_SMOOTHING,
                ),
                4,
            )

    if sub_iso:
        record["last_seen_iso"] = sub_iso

    return record


def update_profile(
    experiment_dir: Path,
    *,
    cluster: str,
    observed_jobs: Iterable[dict[str, Any]],
) -> None:
    """Read existing profile JSON, fold in *observed_jobs*, atomic-write.

    Each observation must carry at minimum a ``user`` field; everything
    else is best-effort. Per-user records that already exist are
    updated in place — :data:`_MEDIAN_SMOOTHING` controls how strongly
    new observations move the running medians.
    """
    obs_list = [o for o in observed_jobs if o.get("user")]
    if not obs_list:
        return
    path = user_profiles_path(experiment_dir, cluster)

    def _mutate(raw: dict[str, Any] | None) -> dict[str, Any]:
        doc = _empty_doc(cluster) if not isinstance(raw, dict) else raw
        doc.setdefault("schema_version", SCHEMA_VERSION)
        doc.setdefault("cluster", cluster)
        users = doc.setdefault("users", {})
        if not isinstance(users, dict):
            users = {}
        for obs in obs_list:
            user = str(obs["user"])
            record = _coerce_user_dict(users.get(user), user)
            users[user] = _fold_observation(record, obs)
        doc["users"] = users
        return doc

    atomic_locked_update(path, _mutate)


def read_profile(
    experiment_dir: Path,
    *,
    cluster: str,
    user: str,
) -> UserProfile | None:
    """Return the :class:`UserProfile` for *user*, or ``None`` if absent."""
    profiles = all_profiles(experiment_dir, cluster=cluster)
    return profiles.get(user)


def all_profiles(experiment_dir: Path, *, cluster: str) -> dict[str, UserProfile]:
    """Return ``{username: UserProfile}`` for the cluster.

    Empty dict when the file does not exist or has been wiped.
    """
    path = user_profiles_path(experiment_dir, cluster)
    if not path.exists():
        return {}
    try:
        text = path.read_text()
    except OSError:
        return {}
    import json

    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(doc, dict):
        return {}
    users = doc.get("users") or {}
    if not isinstance(users, dict):
        return {}
    out: dict[str, UserProfile] = {}
    for user, record in users.items():
        if not isinstance(record, dict):
            continue
        out[user] = _to_profile(record)
    return out


def _to_dict(profile: UserProfile) -> dict[str, Any]:
    """dataclass → dict for serialization (used by tests)."""
    d = asdict(profile)
    # Round float fields for stable JSON.
    for k in ("median_submits_per_day", "median_actual_over_ask",
              "failure_rate", "p_followup_within_6h"):
        if k in d and isinstance(d[k], float):
            d[k] = round(d[k], 6) if not math.isnan(d[k]) else 0.0
    return d
