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


class _AnomalyPolicy(BaseModel):
    """Campaign anomaly-handling policy (design §4: the greenlit spec's
    ``anomaly policy``).

    Every field mirrors a control ``campaign-advance`` already enforces —
    no speculative knobs. The block is written once at campaign start and
    read (never mutated) by ``campaign-advance``.
    """

    model_config = ConfigDict(extra="forbid")

    on_anomaly: Literal["surface", "park"] = Field(
        default="surface",
        description=(
            "How ``campaign-advance`` frames a tripped loud-fail guard in its "
            "``anomaly_brief``: ``surface`` (default) recommends surfacing the "
            "drafted brief for a human ``y``/nudge decision; ``park`` recommends "
            "halting until a human intervenes. Data only — the framework never "
            "acts autonomously on either; it only shapes the brief's "
            "recommendation."
        ),
    )
    resubmit_cap: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-task campaign resubmit backstop, mirroring "
            "``stop_criteria.max_task_resubmits``. Consulted by "
            "``campaign-advance`` only when neither the explicit "
            "``--max-task-resubmits`` nor ``stop_criteria.max_task_resubmits`` is "
            "set; when it too is absent the framework backstop (2) still fires. "
            "Set it to raise/lower the backstop."
        ),
    )
    circuit_breaker_failures: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Consecutive-failure circuit breaker, mirroring "
            "``stop_criteria.circuit_breaker_failures``. Consulted by "
            "``campaign-advance`` only when neither the explicit arg nor "
            "``stop_criteria.circuit_breaker_failures`` is set. No framework "
            "default — omitted keeps the breaker off."
        ),
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
    anomaly_policy: _AnomalyPolicy | None = None
    greenlit: bool = Field(
        default=False,
        description=(
            "Provenance marker: the campaign spec was greenlit once at start "
            "(design §4 — 'drafted and greenlit once, at campaign start'). A "
            "durable DATA flag, NOT an execution gate — no primitive blocks on "
            "it; it records that the greenlight happened. Stamped via "
            "``manifest.mark_greenlit``. Default ``False`` leaves a non-greenlit "
            "manifest's bytes unchanged."
        ),
    )
    greenlit_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 UTC timestamp the spec was greenlit; ``None`` until "
            "``manifest.mark_greenlit`` stamps it."
        ),
    )
    async_refill: bool = Field(
        default=False,
        description=(
            "Opt into continuous-async refill (#362). When set, the campaign "
            "keeps up to ``max_in_flight`` iterations in flight, telling "
            "results as they land and refilling empty slots, instead of the "
            "default staged barrier (one iteration drains before the next is "
            "proposed). ``campaign-advance`` emits a ``refill`` decision and "
            "``load-context`` routes a decide/refill step even while runs are "
            "in flight. Default ``False`` keeps today's synchronous behavior "
            "byte-identical."
        ),
    )
    max_in_flight: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Pool-occupancy target K for ``async_refill``. ``campaign-advance`` "
            "refills until ``in_flight`` reaches this many (capped by budget "
            "headroom). Ignored when ``async_refill`` is false. ``None`` with "
            "``async_refill`` set falls back to the framework default K."
        ),
    )
