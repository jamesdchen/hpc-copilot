"""Pydantic model for ``<campaign_dir>/manifest.json``.

Audit record. The framework only stores fields it can independently
act on (budget, stop_criteria via campaign-budget /
campaign-converged) plus opaque context (goal, strategy.params).
Never required by primitives — purely descriptive.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import OptimizationDirection, PlateauMode


class _CampaignBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_jobs: int | None = Field(default=None, ge=0)
    max_tasks: int | None = Field(default=None, ge=0)
    max_walltime_sec: int | None = Field(default=None, ge=0)
    max_core_hours: float | None = Field(default=None, ge=0)


class _StopCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iters: int | None = Field(default=None, ge=0)
    metric: str | None = None
    target: float | None = None
    direction: OptimizationDirection | None = None
    plateau_window: int | None = Field(default=None, ge=1)
    plateau_tolerance: float | None = Field(default=None, ge=0)
    plateau_mode: PlateauMode | None = Field(
        default=None,
        description=(
            "Plateau baseline. ``all_time_best`` (default): fires when the "
            "recent window failed to beat the all-time prior best — 'no "
            "new record in N iters'. ``prior_window``: fires when the "
            "recent window failed to beat the prior window of equal size "
            "— 'improvements have stalled'. See campaign_converged for "
            "the data-requirement and use-case trade-offs."
        ),
    )
    circuit_breaker_failures: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Loop-safety circuit breaker. ``campaign-advance`` emits "
            "``stop_circuit_breaker`` when this many of the most recent "
            "iterations failed consecutively (terminal failed/abandoned "
            "runs, in submit order). No framework default — omitted means "
            "no breaker."
        ),
    )
    max_task_resubmits: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Loop-safety resubmit cap. ``campaign-advance`` emits "
            "``stop_resubmit_cap`` when any single task slot has accrued this "
            "many resubmit attempts summed across all the campaign's runs — "
            "the campaign-level extension of the within-run auto-retry cap. "
            "No framework default — omitted means no cap."
        ),
    )


class _Strategy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Free string for human/agent display only; framework never dispatches on this.",
    )
    params: dict[str, Any] | None = Field(
        default=None,
        description="Opaque to the framework. Round-tripped untouched.",
    )


class CampaignManifest(BaseModel):
    """Schema for ``<campaign_dir>/manifest.json``."""

    model_config = ConfigDict(extra="forbid", title="campaign manifest")

    manifest_schema_version: Literal[1]
    campaign_id: str = Field(min_length=1)
    created_at: str | None = Field(
        default=None,
        description="ISO-8601 timestamp.",
    )
    goal: str | None = Field(
        default=None,
        description="Free-form prose; framework treats as opaque text (not parsed).",
    )
    budget: _CampaignBudget | None = None
    stop_criteria: _StopCriteria | None = None
    strategy: _Strategy | None = None
