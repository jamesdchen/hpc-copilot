"""Pydantic models for the ``predict-queue-wait`` query atom's wire contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PredictQueueWaitSpec(BaseModel):
    """Inputs for the ``predict-queue-wait`` primitive.

    Phase 4 of the queue-wait predictor: when prerequisites are
    present, dispatches to a discrete-event simulator (DES) backend
    that runs the FIFO + EASY-backfill scheduler forward against the
    most recent persisted ClusterSnapshot.
    """

    model_config = ConfigDict(extra="forbid", title="predict-queue-wait input")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    at_iso: str | None = Field(
        default=None,
        description="Reference timestamp the forecast is for; null means now.",
    )
    backend: Literal["auto", "diurnal_ma", "des"] = "auto"
    n_replications: int = Field(default=64, ge=1)
    seed: int | None = Field(
        default=None,
        description="Optional seed for deterministic DES sampling.",
    )


class PredictQueueWaitResult(BaseModel):
    """Mirrors ``PredictionResult.to_dict()``."""

    model_config = ConfigDict(extra="forbid", title="predict-queue-wait output data")

    predicted_wait_sec: int | None = Field(
        description="Median (p50) predicted wait in seconds. null when the prediction is cold.",
    )
    confidence: Literal["high", "medium", "low", "cold"]
    method: Literal[
        "diurnal_ma",
        "blended_ma",
        "global_ma",
        "no_data",
        "des",
        "des_no_snapshot",
        "des_no_profiles",
    ] = Field(
        description=(
            "Which backend produced the prediction. 'des*' tags "
            "indicate the DES backend ran or attempted to run; "
            "'diurnal_ma'/'blended_ma'/'global_ma' tag the v1 "
            "baseline tiers; 'no_data' is cold start."
        ),
    )
    n_bucket_samples: int = Field(ge=0)
    n_total_samples: int = Field(ge=0)
    bucket_hour_of_week: int = Field(ge=-1, le=167)
    fallback_reason: str | None
    features_adjustment_factor: float = Field(
        description="Order-book adjustment factor (Phase 1c). 1.0 on the wire-driven path and on the DES path; only non-1.0 for internal callers that pass live QueueFeatures directly (not exposed on the spec).",
    )
    p10_wait_sec: int | None = Field(
        description="DES p10 quantile in seconds. null on the diurnal_ma path.",
    )
    p90_wait_sec: int | None = Field(
        description="DES p90 quantile in seconds. null on the diurnal_ma path.",
    )
    n_replications: int | None = Field(
        description="Number of DES passes executed. null on the diurnal_ma path.",
    )
