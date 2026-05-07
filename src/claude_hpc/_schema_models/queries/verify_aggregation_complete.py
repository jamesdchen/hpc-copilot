"""Pydantic model for the ``verify-aggregation-complete`` query atom's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from claude_hpc._schema_models._shared import RunIdLoose


class VerifyAggregationCompleteResult(BaseModel):
    """Post-aggregate invariant report.

    Agent reads ``ok`` and surfaces violations
    (missing_waves / missing_tasks / unexpected_tasks /
    provenance_present) to the user.
    """

    model_config = ConfigDict(title="verify-aggregation-complete output")

    ok: bool
    run_id: RunIdLoose
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
    expected_wave_count: int = Field(ge=0)
    pulled_wave_count: int = Field(ge=0)
    expected_task_count: int = Field(ge=0)
    pulled_task_count: int = Field(ge=0)
