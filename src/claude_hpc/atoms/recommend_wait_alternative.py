"""``recommend-wait-alternative`` primitive — quantify the do-nothing alternative.

Lesson 5 from the SLURM-backfill session: age priority climbs slowly
but reliably. The recommender currently surfaces only "submit at hour
H" via the diurnal MA; this primitive surfaces the missing comparison
arm: "wait N hours, your priority climbs to P." The agent compares
both alternatives and picks whichever has the lower expected wait.

Per the conversation, the recommender's default expectation is that
waiting wins — hour-H beats waiting only when there's a real diurnal
shadow. This primitive doesn't pick; it quantifies the wait arm so
the agent has both numbers.

Pure local primitive — caller fetches priority observations (from
sacct or scontrol) and passes them in. No SSH side effects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc._internal.primitive import primitive
from claude_hpc._schema_models.queries.recommend_wait_alternative import (
    RecommendWaitAlternativeResult,
    RecommendWaitAlternativeSpec,
    _ForecastEntry,
)
from claude_hpc.forecast.age_priority_climb import (
    PrioritySample,
    estimate_climb_rate,
    forecast_priority_after,
)

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="recommend-wait-alternative",
    verb="query",
    side_effects=[],
    idempotent=True,
    agent_facing=True,
)
def recommend_wait_alternative(
    experiment_dir: Path,  # noqa: ARG001 — convention: every atom takes experiment_dir
    *,
    spec: RecommendWaitAlternativeSpec,
) -> RecommendWaitAlternativeResult:
    """Fit a climb rate from priority samples + forecast each horizon.

    Returns a :class:`RecommendWaitAlternativeResult` whose
    ``forecasts`` list pairs each requested wait horizon with the
    expected climbed priority. ``method`` tells the agent how much
    to trust the forecast:

    * ``linear_regression`` — 3+ samples, OLS slope, trustworthy.
    * ``two_point`` — exactly 2 samples, single-segment estimate.
    * ``insufficient_data`` — <2 samples, rate is zero, agent should
      surface "no data" rather than "wait N hours."
    * ``reset_observed`` — observed priority went DOWN between samples
      (job got resubmitted / reservation pinned). Rate clamped to zero;
      agent shouldn't trust the forecast.
    """
    samples = [
        PrioritySample(observed_at_iso=s.observed_at_iso, priority=s.priority) for s in spec.samples
    ]
    climb = estimate_climb_rate(samples)
    forecasts: list[_ForecastEntry] = []
    if climb.rate_priority_per_hour > 0:
        for h in spec.wait_horizons_hours:
            if h <= 0:
                continue
            forecasts.append(
                _ForecastEntry(
                    wait_hours=h,
                    forecast_priority=forecast_priority_after(
                        current_priority=spec.current_priority,
                        climb=climb,
                        hours=h,
                    ),
                )
            )
    return RecommendWaitAlternativeResult(
        rate_priority_per_hour=climb.rate_priority_per_hour,
        method=climb.method,
        n_samples=climb.n_samples,
        forecasts=forecasts,
    )
