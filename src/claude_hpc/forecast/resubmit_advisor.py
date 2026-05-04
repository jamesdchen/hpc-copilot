"""Advisory wire from resubmit into the queue-wait forecaster.

Today the resubmit path (``hpc-mapreduce resubmit``) is purely
mechanical: the caller decides *when* to re-queue and the resubmitter
just rewrites the sidecar + reissues the array.  When the failure was
``preempted`` we already advise the caller to throttle, but for every
other category we silently re-queue at the current time — even when
the queue-wait baseline says the next hour is much cheaper.

:func:`recommend_resubmit_window` closes that gap.  It is pure and
opt-in: callers (currently :func:`claude_hpc.agent_cli.cmd_resubmit` when
``consult_forecast: true`` is set on the spec) compare the "submit
now" predicted wait against the best window in the next horizon and
get back a single recommendation dict.  The advisor never blocks the
resubmit — the agent or user decides what to do with the advice.

Cold-start safety: if either side of the comparison is unavailable
(no runtime-prior samples, unparseable timestamps) the recommendation
falls back to ``"unknown"`` with ``savings_sec=None`` rather than
forcing a brittle decision on the caller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from claude_hpc.forecast.best_submit_window import WindowCandidate, best_submit_windows
from claude_hpc.forecast.queue_wait_baseline import predict_queue_wait

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ResubmitRecommendation", "recommend_resubmit_window"]


Recommendation = Literal["now", "wait", "unknown"]


@dataclass(frozen=True)
class ResubmitRecommendation:
    """Advisory output of :func:`recommend_resubmit_window`.

    ``recommendation`` is the advisor's verdict:

    * ``"now"`` — submitting immediately is no worse than waiting, or
      the projected savings are below ``savings_threshold_sec``.
    * ``"wait"`` — the best near-future window beats now by at least
      the threshold; ``best_window`` is populated.
    * ``"unknown"`` — predictor returned cold-start on either side;
      caller should treat as "submit now" but knows it's blind.
    """

    recommendation: Recommendation
    submit_now_wait_sec: int | None
    best_window: WindowCandidate | None
    savings_sec: int | None
    within_hours: int
    savings_threshold_sec: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["best_window"] = self.best_window.to_dict() if self.best_window else None
        return d


def recommend_resubmit_window(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    within_hours: int = 24,
    savings_threshold_sec: int = 300,
) -> ResubmitRecommendation:
    """Compare "submit now" wait against the best window in the next horizon.

    Parameters
    ----------
    experiment_dir:
        Same dir the resubmit operates on; used to locate the runtime
        prior pool the predictors read.
    profile, cluster:
        Bucketing keys for the queue-wait baseline.  Pulled from the
        run sidecar by the caller.
    within_hours:
        Horizon over which to scan for a better window.  Forwarded to
        :func:`~claude_hpc.forecast.best_submit_window.best_submit_windows`.
    savings_threshold_sec:
        Minimum savings (now − best_window) required to recommend
        ``"wait"``.  Default 5 minutes — small enough to surface real
        diurnal arbitrage, large enough that single-bucket noise won't
        flap the recommendation.
    """
    now_pred = predict_queue_wait(
        experiment_dir,
        profile=profile,
        cluster=cluster,
        at_iso=None,
    )
    submit_now_wait = now_pred.predicted_wait_sec

    candidates = best_submit_windows(
        experiment_dir,
        profile=profile,
        cluster=cluster,
        within_hours=within_hours,
        top_k=1,
    )
    best = candidates[0] if candidates else None

    if submit_now_wait is None or best is None:
        return ResubmitRecommendation(
            recommendation="unknown",
            submit_now_wait_sec=submit_now_wait,
            best_window=best,
            savings_sec=None,
            within_hours=within_hours,
            savings_threshold_sec=savings_threshold_sec,
        )

    savings = submit_now_wait - best.predicted_wait_sec
    verdict: Recommendation = "wait" if savings >= savings_threshold_sec else "now"
    return ResubmitRecommendation(
        recommendation=verdict,
        submit_now_wait_sec=submit_now_wait,
        best_window=best,
        savings_sec=savings,
        within_hours=within_hours,
        savings_threshold_sec=savings_threshold_sec,
    )
