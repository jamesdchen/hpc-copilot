"""Pydantic models for the ``recall`` query atom's wire contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RecallSpec(BaseModel):
    """Optional spec file for ``hpc-agent recall``.

    The same arguments are exposed as CLI flags and the operator
    picks one path. When ``root`` is omitted, recall falls back to
    ``~/.claude-hpc/config.json:experiment_roots`` — both empty
    raises spec_invalid.
    """

    model_config = ConfigDict(extra="forbid", title="recall query spec")

    root: str | None = Field(
        default=None,
        min_length=1,
        description="Filesystem directory to walk (recursively) for interview.json files.",
    )
    task_kind: str | None = Field(
        default=None,
        description="Exact-match filter against intent.task_kind.",
    )
    operator: str | None = Field(
        default=None,
        description="Exact-match filter against intent.produced_by.operator.",
    )
    since: str | None = Field(
        default=None,
        description="Only return campaigns with _materialized.at >= this ISO-8601 timestamp.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        description="Cap on results returned. The total match count (pre-truncation) is reported via data.total_matching.",
    )
    include_runtime: bool = Field(
        default=False,
        description="Tier 2 rollup: walks each matched campaign's .hpc/runtimes/*.json and aggregates elapsed_sec + failure rate.",
    )
    include_generator_stats: bool = Field(
        default=False,
        description="Tier 3 rollup: buckets matched campaigns by task_generator.kind.",
    )


class _CampaignSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_dir: str
    materialized_at: str | None = Field(
        description="ISO-8601 timestamp from interview.json._materialized.at.",
    )
    goal: str | None = None
    task_kind: str | None = None
    task_count: int | None = Field(default=None, ge=0)
    operator: str | None = None
    produced_by_kind: Literal["agent", "human"] | None = None
    cmd_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{8,64}$")
    budget: dict[str, Any] | None = None
    abort_if: dict[str, Any] | None = None
    cluster_target: dict[str, Any] | None = None
    task_generator: dict[str, Any] | None = None


class _TaskCountStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p50: float
    p95: float
    min: int = Field(ge=0)
    max: int = Field(ge=0)


class _MaterializedAtStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    earliest: str
    latest: str


class _WalltimeStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p50: float
    p95: float
    min: int
    max: int
    n_samples: int = Field(ge=0)


class _RuntimeRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    walltime_per_task_sec: _WalltimeStats | None
    failure_rate: float | None = Field(default=None, ge=0, le=1)
    total_task_samples: int = Field(ge=0)
    campaigns_with_no_runtime: int = Field(ge=0)


class _GeneratorByKindEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    count: int = Field(ge=1)
    param_envelopes: dict[str, Any] | None = None
    axis_value_unions: dict[str, Any] | None = None


class _GeneratorRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by_kind: dict[str, _GeneratorByKindEntry]


class _RecallRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)
    task_kind_distribution: dict[str, int]
    operators: dict[str, int]
    produced_by_kinds: dict[str, int]
    task_generator_kinds: dict[str, int]
    clusters: dict[str, int]
    task_count: _TaskCountStats | None
    materialized_at: _MaterializedAtStats | None
    runtime_rollup: _RuntimeRollup | None = None
    generator_rollup: _GeneratorRollup | None = None


class _RecallData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaigns: list[_CampaignSummary]
    total_matching: int = Field(ge=0)
    showing: int = Field(ge=0)
    rollup: _RecallRollup


class RecallEnvelope(BaseModel):
    """Envelope returned by ``hpc-agent recall``.

    Each campaign summary projects the prior-decision fields the
    next interviewer would compare against. The rollup block
    pre-computes cross-campaign aggregations.
    """

    model_config = ConfigDict(extra="forbid", title="recall output envelope")

    ok: Literal[True]
    data: _RecallData
