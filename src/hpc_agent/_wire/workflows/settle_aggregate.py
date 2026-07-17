"""Pydantic models for the ``settle-aggregate`` workflow primitive.

``settle-aggregate`` is the provenance HOME for an operator-bypass table (the
run-13 record-8 class — a table produced OUTSIDE the sanctioned flow, so it has
no aggregate record, no harvest receipt, and journal provenance is LOST;
``docs/design/history/run13-findings.md`` finding 14). It extends the
``settle-run`` directed-evidence pattern to the AGGREGATE stage: given a table
artifact + the runs the human claims it derives from + a typed human utterance
naming the artifact, it journals a directed aggregate settle — RECORDING, never
gating.

It NEVER blesses the numbers: the journaled record says ``operator-settled,
provenance human-asserted``. It NEVER synthesizes consent — the utterance must be
human-authored (the same harness-captured evidence tier ``append-decision``'s
human-authorship gate uses; an agent-composed utterance is refused, not silently
accepted). It only VALIDATES shape (the artifact exists → its sha256 is computed
at record time; the named runs exist) and JOURNALS the human's utterance. Once
journaled, ``verify-relay`` treats the named contributing ids as authorized via
its normal auth-id join, so a truthful relay of the operator-settled table's
run-set is no longer flagged.

I/O contracts:

* Input: ``schemas/settle_aggregate.input.json`` (from ``SettleAggregateInput``).
* Output: ``schemas/settle_aggregate.output.json`` (from ``SettleAggregateResult``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SettleAggregateInput(BaseModel):
    """Inputs to ``settle-aggregate``: the table + its derives-from set + consent.

    ``run_id`` is the run scope the settle is journaled under (the run the table
    is cited under — it need not have gone through the sanctioned flow).
    ``aggregate_ref`` is the path to the table artifact; it MUST exist (an absent
    artifact is refused) so its sha256 can be computed at record time.
    ``derives_from`` is the run-set the human claims the table derives from; every
    named run MUST exist (a record or sidecar) — the settle records a
    human-asserted lineage, it does not invent runs. ``utterance`` is the human's
    typed consent naming the artifact — REQUIRED and human-authored (an
    agent-composed utterance is refused). ``provenance`` optionally notes how the
    settle was captured.
    """

    model_config = ConfigDict(extra="forbid", title="settle-aggregate input spec")

    run_id: str = Field(
        min_length=1,
        description="The run scope the aggregate settle is journaled under (the table's citation).",
    )
    aggregate_ref: str = Field(
        min_length=1,
        description=(
            "Path to the table artifact. MUST exist — its sha256 is computed at "
            "record time and journaled; an absent artifact is refused."
        ),
    )
    derives_from: list[str] = Field(
        min_length=1,
        description=(
            "The run-set the human claims the table derives from; every named run "
            "must exist. Recorded as the settle's contributing_run_ids."
        ),
    )
    utterance: str = Field(
        min_length=1,
        description=(
            "The human's typed consent naming the artifact (REQUIRED, human-"
            "authored). An agent-composed utterance is refused — the verb never "
            "synthesizes consent."
        ),
    )
    provenance: str | None = Field(
        default=None,
        description="Optional note on how the directed settle was captured.",
    )


class SettleAggregateResult(BaseModel):
    """The aggregate-settle outcome — the journaled human-directed record.

    ``artifact_sha256`` is the sha computed over the table's bytes at record time
    (a hash is never asserted into existence). ``contributing_run_ids`` echoes the
    human-asserted derives-from set now authorized for ``verify-relay``.
    ``authorship`` names the evidence tier the utterance cleared
    (``harness-captured`` when the utterance log verified it, ``unverified-
    fallback`` when no log existed — the friction posture, disclosed not hidden).
    The record says ``operator-settled, provenance human-asserted`` — the numbers
    are NEVER blessed.
    """

    model_config = ConfigDict(extra="forbid", title="settle-aggregate output data")

    stage_reached: Literal["settled"] = Field(
        description="Always 'settled' — the directed aggregate settle was journaled.",
    )
    run_id: str = Field(description="The run scope the settle was journaled under.")
    aggregate_ref: str = Field(description="The settled table artifact path.")
    artifact_sha256: str = Field(
        description="sha256 over the table's bytes, computed at record time (64-hex).",
    )
    contributing_run_ids: list[str] = Field(
        default_factory=list,
        description="The human-asserted derives-from run-set now authorized for verify-relay.",
    )
    authorship: str = Field(
        description="The evidence tier the utterance cleared ('harness-captured' / 'unverified-fallback').",
    )
    decision_ts: str = Field(
        description="Timestamp of the journaled directed-aggregate-settle record.",
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the settle outcome.",
    )
