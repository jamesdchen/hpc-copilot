"""Pydantic models for the ``run-story`` query verb (run-story D5).

Wire surface over the run-story projection (:mod:`hpc_agent.state.run_story` +
:mod:`hpc_agent.ops.story_render`). ``run-story`` is a PURE READ: it merges a
run's complete journal trail — decision journal, briefs, block terminals,
journal-record stamps + verdict history, scope journals + look ledgers, notebook
journal — into one deterministic, ordered, attributed timeline, fingerprinted by
``story_sha``.

Boundary posture (mirrors :mod:`hpc_agent._wire.queries.notebook_status` — flat,
no domain vocabulary in field names): an event is IDENTITY (which
run/scope/section), ORDERING (recorded ts), and COUNTING (sha pointers, row/job
counts) over opaque records — never what any record MEANS. The models carry
ids, shas, the closed stream/actor vocabulary, and honest omission counts —
nothing about the experiment's semantics. ``evidence`` is an opaque pointer/count
mapping; ``text`` is the human's verbatim words only (agent prose never rides the
wire — only its digest).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunStorySpec(BaseModel):
    """Inputs to ``run-story``.

    ``run_id`` names the run whose timeline is rendered; ``include_lineage``
    widens the read to the run's whole supersession chain. ``since_ts`` and
    ``limit`` are the honest windowing controls (D6) — a lexicographic ts floor
    and a newest-last cap; a window never masquerades as the whole story
    (``total_events`` / ``omitted_count`` surface the omission).
    """

    model_config = ConfigDict(extra="forbid", title="run-story input spec")

    run_id: str = Field(
        min_length=1,
        description="The run whose complete journal trail is merged into one timeline.",
    )
    include_lineage: bool = Field(
        default=False,
        description=(
            "Widen the read to the run's whole supersession lineage (the one "
            "lineage_chain walk), not just the single run."
        ),
    )
    since_ts: str | None = Field(
        default=None,
        description=(
            "Lexicographic ISO-8601 timestamp floor — keep only events at or after "
            "this ts. Omitted events are counted, never silently dropped."
        ),
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Keep only the most recent N events (newest-last window). The omission "
            "count is a rendered, countable fact."
        ),
    )
    markdown: bool = Field(
        default=True,
        description="Include the code-rendered markdown timeline in the result.",
    )


class RunStoryEvent(BaseModel):
    """One typed, attributed timeline entry (the D3 event model).

    Every field is IDENTITY / ORDERING / COUNTING — never domain semantics.
    ``evidence`` carries sha pointers + counts only; ``text`` is the human's
    verbatim words (a nudge response, an unlock reason) or ``""``.
    """

    model_config = ConfigDict(extra="forbid", title="run-story event")

    ts: str = Field(description="Recorded ISO-8601 timestamp verbatim, or '' when absent.")
    stream: str = Field(description="The source-store noun the event came from.")
    actor: str = Field(description="'human' or 'code' — who authored the act.")
    kind: str = Field(description="The record-class literal (block name, 'look', 'verdict', ...).")
    subject_id: str = Field(description="run_id / scope tag / audit section — opaque identity.")
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Sha pointers + counts only (never a metric value).",
    )
    text: str = Field(
        default="",
        description="The human's verbatim words when the record carries any, else ''.",
    )


class RunStoryResult(BaseModel):
    """The ordered timeline + its fingerprint + honest omission counts.

    ``story_sha`` is a deterministic fingerprint over the canonical JSON of the
    WINDOWED story (header + events + counts), so it can never be passed off as
    covering events a window dropped. ``total_events`` is the full count before
    windowing; ``omitted_count`` is what a window dropped.
    """

    model_config = ConfigDict(extra="forbid", title="run-story output data")

    run_ids: list[str] = Field(
        default_factory=list,
        description="The run id(s) the timeline covers (one, or a lineage chain).",
    )
    events: list[RunStoryEvent] = Field(
        default_factory=list,
        description="The merged, ordered, windowed timeline.",
    )
    story_sha: str = Field(description="sha256 fingerprint over the windowed canonical JSON.")
    markdown: str = Field(
        default="",
        description="The code-rendered markdown timeline (empty when not requested).",
    )
    total_events: int = Field(
        default=0,
        description="Full event count before any window (>= len(events)).",
    )
    omitted_count: int = Field(
        default=0,
        description="Events a window dropped — a rendered, countable fact (never silent).",
    )
