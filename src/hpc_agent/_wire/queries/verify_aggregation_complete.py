"""Pydantic model for the ``verify-aggregation-complete`` query atom's output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class VerifyAggregationCompleteResult(BaseModel):
    """Post-aggregate invariant report.

    Agent reads ``ok`` and surfaces violations
    (missing_waves / missing_tasks / unexpected_tasks /
    provenance_present) to the user.
    """

    model_config = ConfigDict(extra="forbid", title="verify-aggregation-complete output")

    ok: bool
    run_id: RunIdStrict
    all_waves_combined: bool
    missing_waves: list[int]
    all_tasks_present: bool
    missing_tasks: list[int]
    unexpected_tasks: list[int] = Field(
        description="Cross-run contamination: task_ids in pulled partials but not in this run's wave_map.",
    )
    unexpected_aggregated_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Post-reduce contamination: keys in the supplied "
            "``aggregated_metrics`` dict that don't match any grid-point "
            "produced by ``tasks.resolve(i)`` for i in [0, total_tasks). "
            "Only populated when the caller supplied "
            "``aggregated_metrics`` + ``aggregated_keying='grid_point'``; "
            "empty list otherwise."
        ),
    )
    provenance_present: bool
    columns_checked: bool = Field(
        default=False,
        description=(
            "True when the expected-columns / non-NaN-metric gate ran — "
            "the run sidecar's ``results`` block declared "
            "``expected_columns`` and/or ``metric_column`` AND a local "
            "results directory was supplied. False = clean no-op skip."
        ),
    )
    column_violations: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Per-result-file violations from the columns gate: each "
            "entry is {path, missing_columns, metric_nan, "
            "metric_nan_rows, error}. Empty when the gate passed or was "
            "skipped."
        ),
    )
    expected_wave_count: int = Field(ge=0)
    pulled_wave_count: int = Field(ge=0)
    expected_task_count: int = Field(ge=0)
    pulled_task_count: int = Field(ge=0)
