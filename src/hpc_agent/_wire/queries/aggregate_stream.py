"""Pydantic models for the ``aggregate-stream`` query verb (streaming-aggregate SPEC §1).

``aggregate-stream`` is a read-mostly, re-callable QUERY: given one run OR a set
of parent run_ids, it censuses per-arm completeness, reduces ONLY the complete
arms through the run's own deterministic reducer, and emits a partial-but-honest
aggregate that discloses every pending arm BY NAME (never a silent cap). It
actuates nothing — no submit, no kill, no journal terminal — and each call
supersedes the prior snapshot with more arms (monotonic ``snapshot_seq``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import RunIdStrict


class AggregateStreamInput(BaseModel):
    """What to stream: exactly ONE of a single run or a set of parent runs."""

    model_config = ConfigDict(extra="forbid", title="aggregate-stream input spec")

    run_id: RunIdStrict | None = Field(
        default=None,
        description="Stream a single run's arms. Mutually exclusive with parents.",
    )
    parents: list[RunIdStrict] | None = Field(
        default=None,
        description=(
            "Stream a multi-leg run — each parent owns its arm space (the lgbm-leg "
            "+ xgb-leg progressive table). Mutually exclusive with run_id."
        ),
    )
    output_dir: str | None = Field(
        default=None,
        description=(
            "Override the snapshot directory (default "
            "<experiment>/_aggregated/<key>/). The partial metrics_aggregate.json "
            "lands here; a stable key lets each call refine the same snapshot."
        ),
    )
    force: bool = Field(
        default=False,
        description="Reserved: re-pull the mirror even if a local one is present.",
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> AggregateStreamInput:
        has_single = self.run_id is not None
        has_parents = bool(self.parents)
        if has_single == has_parents:
            raise ValueError(
                "aggregate-stream needs EXACTLY ONE of run_id or parents "
                "(a single run OR a multi-leg parent set), never both or neither."
            )
        return self


class StreamArmPending(BaseModel):
    """One still-draining arm, disclosed by name (the never-silent-cap rule)."""

    model_config = ConfigDict(extra="forbid", title="aggregate-stream pending arm")

    arm: str
    tasks_done: int = Field(ge=0)
    tasks_expected: int = Field(ge=0)
    owner_run_id: str = Field(description="The parent run whose leg still owes this arm.")


class AggregateStreamResult(BaseModel):
    """A partial-but-honest aggregate over the arms complete NOW.

    Every number in ``aggregated_metrics`` / ``per_arm_metrics`` is
    reducer-computed (core selected WHICH arms, the reducer computed the values).
    ``arms_pending`` names what is still coming and from where; ``snapshot_seq``
    is monotonic across calls and ``newly_complete`` is the delta since the prior
    snapshot. ``arms_regressed`` names any arm that was complete in a prior
    snapshot but is not now (disclosed, never masked).
    """

    model_config = ConfigDict(extra="forbid", title="aggregate-stream output data")

    ok: bool
    parents: list[str]
    snapshot_seq: int = Field(ge=1)
    superseded: int | None = Field(
        default=None, description="The prior snapshot_seq this call supersedes, or null."
    )
    arms_complete: list[str]
    arms_pending: list[StreamArmPending]
    newly_complete: list[str] = Field(
        description="Arms complete in THIS snapshot that were not in the prior one."
    )
    arms_regressed: list[str] = Field(
        description="Arms complete in a prior snapshot but not now (disclosed, never masked)."
    )
    aggregated_metrics: dict[str, Any] = Field(
        description="Reducer weighted-mean over ALL complete arms' task summaries."
    )
    per_arm_metrics: dict[str, Any] = Field(
        description="Per-complete-arm reducer rows (the progressive table), keyed owner:arm."
    )
    output_path_local: str = Field(
        description="Where the partial metrics_aggregate.json was written."
    )
    reduce_path: str = Field(
        description="'builtin' (per-arm weighted-mean) or 'ownership' (multi-parent dedup)."
    )
    ownership_dedup: dict[str, Any] | None = Field(
        default=None,
        description="Multi-parent ownership accounting (raced cells dropped to their owner), or null.",
    )
    disagreement: dict[str, Any] | None = Field(
        default=None,
        description="Announce-vs-status-reporter census disagreement per parent, or null.",
    )
    reduced_at: str
