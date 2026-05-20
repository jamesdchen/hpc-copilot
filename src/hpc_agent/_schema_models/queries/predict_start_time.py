"""Wire model for the ``predict-start-time`` primitive.

Pure local primitive: caller fetches squeue + sshare via SSH and
passes the raw text in. Keeps the framework boundary side-effect-
free; the slash command does the cluster I/O.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PredictStartTimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now_iso: str = Field(min_length=1, description="ISO timestamp anchoring the forecast.")
    squeue_text: str = Field(
        description=(
            "Raw output of ``squeue --user='*' -O "
            "'JOBID|PRIORITY|PARTITION|USERNAME|STATE|TIME_LEFT|TIME_LIMIT'``."
        )
    )
    partition: str = Field(min_length=1)
    partition_slot_count: int = Field(ge=1)
    your_priority: int = Field(ge=0)
    your_walltime_sec: int = Field(ge=1)
    your_user: str | None = None
    your_constraint: str = ""
    pending_walltime_default_sec: int = Field(default=86400, ge=1)
    sshare_text: str | None = Field(
        default=None,
        description="Raw ``sshare -P`` output. When absent, fairshare features collapse to sentinels.",
    )
    candidate_offsets_hours: list[float] = Field(
        default_factory=lambda: [0.0, 1.0, 3.0, 6.0, 12.0, 24.0],
        description=(
            "Offsets to evaluate for the wait-vs-submit-now sweep. The "
            "primitive picks whichever offset minimizes total time-to-actual-start."
        ),
    )
    model_path: str | None = Field(
        default=None,
        description=(
            "Path to a serialized LightGBM ``model.txt``. When absent, "
            "predictions fall back to the pessimistic floor (no residual)."
        ),
    )


class _CandidateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset_hours: float
    predicted_iso: str
    floor_pessimistic_iso: str
    floor_optimistic_iso: str
    overhead_sec: int
    total_time_sec: int
    method: str


class PredictStartTimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    best_submit_offset_hours: float
    best_predicted_start_iso: str
    best_total_time_sec: int
    candidates: list[_CandidateOut]
