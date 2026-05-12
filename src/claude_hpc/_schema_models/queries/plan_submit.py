"""Pydantic model for the ``plan-submit`` (score-submit-plan) query atom's output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from claude_hpc._schema_models._shared import Scheduler


class _PlanSubmitCandidate(BaseModel):
    model_config = ConfigDict(extra="allow")

    constraint: str
    pool_size: int | None = Field(default=None, ge=0)
    healthy_nodes: list[str] | None = None
    stressed_nodes: list[str] | None = None
    eta_sec_via_test_only: float | None = None
    runtime_prior_quantiles_sec: dict[str, Any] | None = Field(
        default=None,
        description="Per-gpu_type quantiles from runtime-prior; empty when this candidate has no priors.",
    )
    p_fail_30d: dict[str, float] | None = Field(
        default=None,
        description="Per-gpu_type failure probability over the last 30 days.",
    )


class PlanSubmitResult(BaseModel):
    """Shape of the ``data`` field on a successful ``plan-submit`` envelope.

    The slash command applies the cost rubric (see
    ``docs/primitives/score-submit-plan.md``) to the candidates list
    and picks one constraint.
    """

    model_config = ConfigDict(extra="forbid", title="plan-submit (score-submit-plan) output data")

    profile: str
    cluster: str
    now_iso: str
    scheduler_kind: Scheduler
    needs_canary: bool
    canary_plan: dict[str, Any] | None = Field(
        description="When needs_canary=true, a 1-task probe spec (constraint, walltime). Null when scoring is possible.",
    )
    candidates: list[_PlanSubmitCandidate]
    walltime_arbitraged_from: int | None = Field(
        default=None,
        description="Original walltime ask in seconds before cold-start arbitrage trimmed it. Null when arbitrage didn't fire.",
    )
    walltime_arbitraged_to: int | None = Field(
        default=None,
        description="Cold-start-trimmed walltime in seconds (the value callers should actually use). Null when arbitrage didn't fire.",
    )
    daisy_chain_segments: int | None = Field(
        default=None,
        description="Number of dependency-chained segments the task was split into. Null when no chaining.",
    )
    daisy_chain_segment_walltime_sec: int | None = Field(
        default=None,
        description="Per-segment walltime (post-rebalance) callers should request for each chained segment. Null when no chaining.",
    )
    daisy_chain_total_walltime_sec: int | None = Field(
        default=None,
        description="Sum of per-segment walltimes across the chain (==original ask after rebalance). Null when no chaining.",
    )
    daisy_chain_dep_jobids: list[str] | None = Field(
        default=None,
        description="Scheduler job IDs of each prior segment. Always null at plan_submit time; submit_flow populates it.",
    )
