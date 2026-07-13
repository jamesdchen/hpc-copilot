"""Pydantic models for the ``trace-render`` query verb (data-trace T5).

Wire surface over the trace-render projection
(:mod:`hpc_agent.ops.trace_render_op`). ``trace-render`` is a PURE READ: it
reads ONE task's trace out of the store (:mod:`hpc_agent.state.data_trace`),
joins the run/audit sidecar for a SELF-DESCRIBING header, and renders the FOUR
deterministic markdown views (row waterfall, label-chain line, feature lineage,
sketch table) over the records + the ONE atom-schema registry.

Boundary posture (the ``run_story`` / ``notebook_status`` posture — flat, no
domain vocabulary in field names): a view is IDENTITY (which stage/column/
label), COUNTING (rows/nulls/deltas), and the records' OWN opaque ``{rule,
detail}`` flags — never what any number MEANS. The render carries NO verdict
vocabulary (the never-judgment pin): the trace SHOWS, the scientist concludes
(the pointing doctrine applied to data). Absence is an honest result, never an
error: "no trace recorded for this scope" rides ``present``/``skipped``.

Two selector shapes (exactly one per call):

* DIRECT point lookup — ``{scope_kind, scope_id, task?}`` (Class C exact key).
* REFERENCE lookup — ``{cmd_sha}`` or ``{profile}`` (Class B meaning-adjacent,
  latest-by via a sidecar join; resolves to the newest matching run's trace).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TraceRenderSpec(BaseModel):
    """Inputs to ``trace-render`` — EXACTLY ONE selector shape.

    * ``scope_kind`` + ``scope_id`` — the DIRECT point lookup (Class C): read
      ``.hpc/traces/<scope_kind>/<scope_id>/task-<task>.jsonl`` verbatim.
    * ``cmd_sha`` — the REFERENCE lookup (Class B): the newest run whose sidecar
      records this ``cmd_sha`` (parameter identity), then its run-scope trace.
    * ``profile`` — the REFERENCE lookup by the sidecar's literal ``profile``
      label: the newest run carrying it (``latest-by-profile``, A7 Class B).

    ``task`` selects which per-task trace file (single-task local/audit runs use
    ``task-0``). ``markdown`` toggles the rendered view string in the result.
    """

    model_config = ConfigDict(extra="forbid", title="trace-render input spec")

    scope_kind: Literal["run", "audit", "local"] | None = Field(
        default=None,
        description="Trace store scope kind for the DIRECT point lookup (with scope_id).",
    )
    scope_id: str | None = Field(
        default=None,
        min_length=1,
        description="Trace store scope id for the DIRECT point lookup (with scope_kind).",
    )
    task: int = Field(
        default=0,
        ge=0,
        description="Per-task trace file index (single-task runs use task-0).",
    )
    cmd_sha: str | None = Field(
        default=None,
        min_length=1,
        description="REFERENCE lookup: newest run matching this cmd_sha (Class B).",
    )
    profile: str | None = Field(
        default=None,
        min_length=1,
        description="REFERENCE lookup: newest run carrying this sidecar profile label (Class B).",
    )
    markdown: bool = Field(
        default=True,
        description="Include the code-rendered markdown views in the result.",
    )

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> TraceRenderSpec:
        direct = self.scope_kind is not None or self.scope_id is not None
        if direct and (self.scope_kind is None or self.scope_id is None):
            raise ValueError(
                "scope_kind and scope_id must be provided together for a direct lookup"
            )
        chosen = [direct, self.cmd_sha is not None, self.profile is not None]
        if sum(chosen) != 1:
            raise ValueError(
                "provide EXACTLY ONE selector: {scope_kind + scope_id} | {cmd_sha} | {profile}"
            )
        return self


class TraceWaterfallRow(BaseModel):
    """One row-waterfall stage: rows in/out + declared drops + conservation arithmetic."""

    model_config = ConfigDict(extra="forbid", title="trace waterfall row")

    stage: str = Field(description="The stage name (fine-grained emit).")
    seq: int = Field(description="The stage's emission sequence number.")
    rows_in: int | None = Field(
        default=None, description="Predecessor stage's row_count.rows, or null at the head."
    )
    dropped: int | None = Field(default=None, description="Rows this stage declared dropped.")
    rows_out: int | None = Field(default=None, description="Rows at this stage's exit.")
    expected: int | None = Field(
        default=None, description="rows_in - dropped (the conservation pre-image), or null."
    )


class TraceFeatureRow(BaseModel):
    """One feature-lineage stage: the col_set delta versus the predecessor."""

    model_config = ConfigDict(extra="forbid", title="trace feature-lineage row")

    stage: str = Field(description="The stage name.")
    added: list[str] = Field(
        default_factory=list, description="Columns present here, absent in the predecessor col_set."
    )
    dropped: list[str] = Field(
        default_factory=list, description="Columns present in the predecessor col_set, absent here."
    )


class TraceSketchRow(BaseModel):
    """One (stage, column) sketch cell: null_count + the fixed value_sketch fields."""

    model_config = ConfigDict(extra="forbid", title="trace sketch row")

    stage: str = Field(description="The stage name.")
    column: str = Field(description="The column the sketch/null_count is for.")
    null_count: int | None = Field(default=None, description="Missing values for this column.")
    min: float | None = Field(default=None, description="value_sketch min, or null.")
    mean: float | None = Field(default=None, description="value_sketch mean, or null.")
    std: float | None = Field(default=None, description="value_sketch std, or null.")
    max: float | None = Field(default=None, description="value_sketch max, or null.")
    q05: float | None = Field(default=None, description="value_sketch q05, or null.")
    q50: float | None = Field(default=None, description="value_sketch q50, or null.")
    q95: float | None = Field(default=None, description="value_sketch q95, or null.")


class TraceFlag(BaseModel):
    """One flag in the canonical finding shape (rendered verbatim, never interpreted)."""

    model_config = ConfigDict(extra="forbid", title="trace flag")

    rule: str = Field(description="The flag rule name (the record's OWN word).")
    detail: str = Field(description="The flag detail (the record's OWN word).")
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="Opaque pointers/counts the flag carries."
    )


class TraceRenderResult(BaseModel):
    """The four structured views + the self-describing header + the render string.

    ``present`` is False (and ``skipped`` carries the disclosure) when the
    resolved scope has no recorded trace — an honest result, never an error.
    ``trace_sha`` is a FINGERPRINT over the records (the identity the trace joins
    the trust chain by), ``""`` when absent.
    """

    model_config = ConfigDict(extra="forbid", title="trace-render output data")

    scope_kind: str = Field(description="The resolved trace store scope kind.")
    scope_id: str = Field(description="The resolved trace store scope id ('' when unresolved).")
    task: int = Field(description="The per-task index rendered.")
    resolved_from: str = Field(
        description="How the scope was resolved: 'spec' | 'cmd_sha' | 'profile'."
    )
    present: bool = Field(description="Whether a trace was found for the resolved scope.")
    skipped: str = Field(default="", description="Absence disclosure ('' when a trace is present).")
    stage_count: int = Field(default=0, description="Number of records in the trace.")
    trace_sha: str = Field(
        default="", description="Canonical sha over the records ('' when absent)."
    )
    header: dict[str, Any] = Field(
        default_factory=dict,
        description="Self-describing run/config identity from the sidecar join.",
    )
    waterfall: list[TraceWaterfallRow] = Field(
        default_factory=list, description="View (a): stage-by-stage row waterfall."
    )
    label_chains: dict[str, list[str]] = Field(
        default_factory=dict,
        description="View (b): per tracked label, its 'stage=value' chain across stages.",
    )
    feature_lineage: list[TraceFeatureRow] = Field(
        default_factory=list, description="View (c): per-stage col_set add/drop deltas."
    )
    feature_births: dict[str, str] = Field(
        default_factory=dict, description="View (c): each column mapped to its birth stage."
    )
    sketch: list[TraceSketchRow] = Field(
        default_factory=list, description="View (d): per (stage, column) sketch + null_count."
    )
    flags: list[TraceFlag] = Field(
        default_factory=list,
        description="All flags (generic invariants + record-carried), rendered verbatim.",
    )
    render: str = Field(
        default="", description="The code-rendered markdown views ('' when not requested)."
    )
