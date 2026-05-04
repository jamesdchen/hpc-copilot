"""Conditional residual-lifetime estimator for running jobs.

Phase 2b: given a :class:`UserProfile`, predict how much longer a
currently-running job is likely to keep running. Used by
:mod:`hpc_mapreduce.job.state_forecast` to project the cluster's
free-resource pool ``T`` seconds into the future.

The estimator is a pure function — no I/O, no side effects, deterministic
output for deterministic input — so callers can use it inside hot loops
without worrying about file locks.

Model
-----
Modern users either:

1. Submit with a tight walltime ask and finish well inside it
   (``actual_over_ask < 1``).
2. Submit with a generous walltime ask and use most of it.

We don't try to predict which type a user is on a per-job basis;
instead we just scale the *remaining* walltime budget by the user's
historical ``median_actual_over_ask``. A user who reliably finishes
at 60% of ask gets a residual that's 60% of (ask - elapsed); a user
who hits 100% gets the full remaining budget.

Cold-start (``n_observations < threshold``): fall back to a global
``fallback_ratio`` (configurable). 0.85 is a reasonable default that
tracks population-level studies on slurm clusters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hpc_mapreduce.job.user_profiles import UserProfile

__all__ = ["predict_residual_lifetime", "MIN_OBSERVATIONS_FOR_PROFILE"]

# Threshold below which the estimator falls back to *fallback_ratio*
# rather than trusting the per-user median. Empirical: with ~10
# observations the running blend has had time to move off its 1.0
# seed; below that the profile is too thin to be informative.
MIN_OBSERVATIONS_FOR_PROFILE: int = 10


def predict_residual_lifetime(
    *,
    profile: UserProfile,
    elapsed_sec: int,
    walltime_ask_sec: int,
    fallback_ratio: float = 0.85,
) -> int:
    """Predict the number of additional seconds *job* is likely to run.

    Parameters
    ----------
    profile:
        The :class:`UserProfile` of the user who owns this job.
    elapsed_sec:
        Seconds the job has been running so far.
    walltime_ask_sec:
        Seconds the user requested via ``--time``.
    fallback_ratio:
        Ratio applied to ``(walltime_ask - elapsed)`` when the
        profile is too thin to trust (``n_observations <
        :data:`MIN_OBSERVATIONS_FOR_PROFILE```). Defaults to 0.85 —
        most jobs finish a bit before their ask but rarely overrun.

    Returns
    -------
    Predicted residual lifetime in seconds, clamped to
    ``[0, walltime_ask_sec - elapsed_sec]``. Negative or invalid
    inputs yield 0 — the caller can interpret that as "should have
    completed already".
    """
    elapsed = max(0, int(elapsed_sec))
    ask = max(0, int(walltime_ask_sec))
    remaining = max(0, ask - elapsed)
    if remaining == 0:
        return 0

    if profile.n_observations >= MIN_OBSERVATIONS_FOR_PROFILE:
        ratio = float(profile.median_actual_over_ask)
    else:
        ratio = float(fallback_ratio)
    if ratio <= 0:
        return 0

    # The user's median actual elapsed at completion is `ask * ratio`.
    # If they're already past that point (`elapsed > ask*ratio`),
    # they're in the long tail — but they still cannot exceed `ask`
    # because the scheduler will hard-kill at ask. So:
    #   expected_total = max(elapsed, ask * ratio), capped at ask.
    expected_total = max(elapsed, ask * ratio)
    expected_total = min(expected_total, ask)
    residual = int(round(max(0.0, expected_total - elapsed)))
    return min(residual, remaining)
