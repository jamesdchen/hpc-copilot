"""Pydantic models for the ``best-submit-window`` query atom's wire contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BestSubmitWindowSpec(BaseModel):
    """Inputs for the ``best-submit-window`` primitive."""

    model_config = ConfigDict(extra="forbid", title="best-submit-window input")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    within_hours: int = Field(default=24, ge=1)
    top_k: int = Field(default=5, ge=1)


class _SubmitWindowCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submit_iso: str = Field(description="UTC, second-resolution.")
    predicted_wait_sec: int = Field(ge=0)
    confidence: Literal["high", "medium", "low", "cold"]
    method: str
    n_bucket_samples: int = Field(ge=0)


class BestSubmitWindowResult(BaseModel):
    """Top-k submit-time candidates in ascending predicted-wait order."""

    model_config = ConfigDict(extra="forbid", title="best-submit-window output data")

    profile: str
    cluster: str
    within_hours: int = Field(ge=1)
    top_k: int = Field(ge=1)
    candidates: list[_SubmitWindowCandidate] = Field(
        description="Top-k submit windows ranked ascending by predicted_wait_sec. Empty when the diurnal predictor is cold across every queried hour.",
    )
