"""Wire model for the ``recommend-wait-alternative`` primitive.

Surfaces the "do nothing" alternative to the diurnal-MA "submit at
hour H" recommendation. Per the conversation: the recommender's
default expectation should be wait — your queue position shrinks
faster than any hour-H you'd find in most cases. This primitive
quantifies the climb so the agent can compare both alternatives.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _PrioritySampleSpec(BaseModel):
    """One ``(observed_at_iso, priority)`` observation drawn from a
    past pending job on the same partition / cluster."""

    model_config = ConfigDict(extra="forbid")

    observed_at_iso: str = Field(min_length=1)
    priority: int = Field(ge=0)


class RecommendWaitAlternativeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_priority: int = Field(
        ge=0,
        description="The user's current pending-job priority on the target partition.",
    )
    samples: list[_PrioritySampleSpec] = Field(
        default_factory=list,
        description=(
            "Past (observed_at_iso, priority) observations from pending jobs "
            "on the same partition. Two or more required for a non-trivial "
            "estimate; fewer surfaces ``method=insufficient_data`` and a zero-"
            "rate forecast."
        ),
    )
    wait_horizons_hours: list[float] = Field(
        default_factory=lambda: [1.0, 3.0, 6.0, 12.0, 24.0],
        description=(
            "Wait durations (hours) to forecast the climbed priority for. "
            "The agent surfaces 'wait N hours, your priority climbs to P' "
            "for each horizon."
        ),
    )


class _ForecastEntry(BaseModel):
    """Forecast for one wait-horizon."""

    model_config = ConfigDict(extra="forbid")

    wait_hours: float = Field(ge=0)
    forecast_priority: int = Field(ge=0)


class RecommendWaitAlternativeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate_priority_per_hour: float = Field(
        ge=0,
        description=(
            "Fitted climb rate. Zero when ``method=insufficient_data`` or "
            "``method=reset_observed`` (priority went down — caller should "
            "treat the rate as untrustworthy)."
        ),
    )
    method: Literal["linear_regression", "two_point", "insufficient_data", "reset_observed"]
    n_samples: int = Field(ge=0)
    forecasts: list[_ForecastEntry] = Field(
        default_factory=list,
        description=(
            "One entry per requested ``wait_horizons_hours``. Empty when no "
            "horizons were requested or the rate is zero."
        ),
    )
