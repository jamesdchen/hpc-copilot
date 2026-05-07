"""Pydantic model for the ``read-runtime-prior`` query atom's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _RuntimeQuantiles(BaseModel):
    p50: float = Field(ge=0)
    p95: float = Field(ge=0)
    p99: float | None = Field(default=None, ge=0)
    mean_sec: float | None = Field(default=None, ge=0)
    n_samples: int | None = Field(default=None, ge=0)
    min_sec: float | None = Field(default=None, ge=0)
    max_sec: float | None = Field(default=None, ge=0)


class RuntimePriorResult(BaseModel):
    """Shape of the ``data`` field on a successful ``runtime-prior`` envelope."""

    model_config = ConfigDict(extra="forbid", title="runtime-prior output data")

    profile: str
    cluster: str
    now_iso: str
    needs_canary: bool = Field(
        description=(
            "True when no samples exist for this (profile, cluster) "
            "pair. Caller should submit a 1-task canary, ingest its "
            "result via runtime_prior.append_sample, then re-call."
        ),
    )
    quantiles: dict[str, _RuntimeQuantiles] = Field(
        description="Per-gpu_type runtime quantile rollup. Empty dict when needs_canary is true.",
    )
    total_samples: int = Field(ge=0)
    filtered_by_cmd_sha: str | None = None
