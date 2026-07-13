"""Pydantic models for the campaign human-amplification block verbs.

The campaign flow, decomposed (``docs/design/human-amplification-blocks.md``
§4) into the three touchpoints a campaign actually has. Unlike submit
(S1–S4, a per-run linear chain), a campaign's spec is **greenlit once at
start**, then executes **fully asynchronously** — reconcile ticks self-chain
while healthy; the strategy chooses next batches deterministically; there is
**no per-iteration human boundary**. So the campaign has exactly three
human touchpoints, and exactly three blocks:

* **``campaign-greenlight`` (start).** Read + digest the campaign spec
  (manifest: goal / budget / strategy / stop_criteria / anomaly_policy /
  async_refill) into a spec brief for ``y``/nudge. The verb never decides:
  an un-greenlit manifest digests to ``needs_greenlight`` (nothing stamped);
  a ``confirm`` re-invocation — passed AFTER the human's ``y`` — stamps
  ``mark_greenlit`` and journals the decision; an already-greenlit manifest
  is an idempotent re-read (``already_greenlit``, no decision needed).
* **``campaign-watch`` (async execution surface).** A read-only digest of
  the running campaign for the anomaly / health briefs. Composes the
  journal / index reads and the ``campaign-advance`` evidence — it OBSERVES,
  it never runs a tick (ticks self-chain via the existing driver). Three
  terminators: ``watching_healthy`` (no decision), ``watching_anomaly`` (a
  §5 loud-fail / budget halt → ``y``/nudge, surfacing the ``anomaly_brief``),
  ``watching_complete`` (a stop criterion fired → hand off to
  ``campaign-complete``).
* **``campaign-complete`` (end).** The completion brief — spend vs budget,
  iterations, stop reason, and a code-extracted per-iteration outcome table —
  plus an EMPTY ``proposed_interpretations`` slot the LLM fills at the
  ``y``/nudge boundary. Code extracts the outcomes; the human concludes from
  them (§2). Always a decision terminator.

All three share :class:`CampaignBlockResult`, mirroring the submit blocks'
:class:`~hpc_agent._wire.workflows.submit_blocks.SubmitBlockResult` shape.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The union of every terminator a campaign block can stop at, modelled as data
# (#231 decision-points-as-data). ``needs_decision`` on the result says whether
# a human must answer here; ``stage_reached`` carries the finer detail.
CampaignBlockStage = Literal[
    # greenlight (start).
    "needs_greenlight",  # spec digested; awaiting the once-at-start y/nudge.
    "greenlit",  # a confirm re-invocation stamped the marker + journaled it.
    "already_greenlit",  # idempotent re-read — the marker is already set.
    # watch (async execution surface).
    "watching_healthy",  # campaign nominal (continue / wait_in_flight); no boundary.
    "watching_refill",  # advance decided refill → hand off to campaign-refill (needs_decision=False).
    "watching_anomaly",  # §5 loud-fail guard tripped, or a budget halt → y/nudge.
    "watching_complete",  # a stop criterion fired → hand off to campaign-complete.
    # complete (end).
    "complete",  # completion brief ready; the human interprets the outcomes.
]


class CampaignBlockResult(BaseModel):
    """Shared ``data`` block for every campaign block.

    The ``brief`` is the code-digested evidence the LLM drafts a proposal over
    (§2): the digested spec for greenlight, the ``campaign-advance`` health /
    anomaly evidence for watch, the spend-vs-budget + outcome table for
    complete. ``needs_decision`` marks a ``y``/nudge terminator — True for the
    greenlight brief, an anomaly / budget halt, and the completion brief;
    False for an idempotent already-greenlit re-read, a healthy watch, and a
    watch that merely hands off to ``campaign-complete``.
    """

    model_config = ConfigDict(extra="forbid", title="campaign-block output data")

    block: Literal["greenlight", "watch", "complete"] = Field(
        description="Which campaign block produced this result.",
    )
    stage_reached: CampaignBlockStage = Field(
        description="The terminator the block stopped at (decision-as-data, #231).",
    )
    needs_decision: bool = Field(
        description=(
            "True when a human must answer here (the y/nudge terminator): the "
            "greenlight brief, an anomaly / budget halt, or the completion brief. "
            "False for an already-greenlit re-read, a healthy watch, or a "
            "hand-off-to-complete watch."
        ),
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the terminator.",
    )
    campaign_id: str | None = Field(
        default=None,
        description="The campaign this block operated on.",
    )
    brief: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Code-digested evidence the LLM drafts a proposal over (§2). Shape "
            "varies per block; never interpreted raw by the LLM."
        ),
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The DETERMINISTICALLY-computed next block — ``{verb, why, spec_hint}`` "
            "— or null at a terminal / human-branch terminator (design §2, the "
            "``_next_step_hint`` pattern generalized). ``verb`` names the next "
            "block's CLI verb; ``spec_hint`` carries the minimal next-spec skeleton "
            "(campaign_id). Surfaced, greenlit, journaled, and enforced — never "
            "free-prose."
        ),
    )


class CampaignGreenlightSpec(BaseModel):
    """Inputs to ``campaign-greenlight`` (start).

    A pure read on the first pass (digest the spec → ``needs_greenlight``). On
    the re-invocation the caller makes AFTER the human answered ``y``, set
    ``confirm=True`` — the block stamps ``mark_greenlit`` onto the manifest and
    journals the decision (``response`` / ``proposal``). The verb never decides
    on its own: it digests, or records a caller-supplied greenlight.
    """

    model_config = ConfigDict(extra="forbid", title="campaign-greenlight input spec")

    campaign_id: str = Field(
        min_length=1,
        description="The campaign whose manifest (the greenlit-once spec) to digest.",
    )
    confirm: bool = Field(
        default=False,
        description=(
            "Set on the post-`y` re-invocation to RECORD the human's greenlight: "
            "stamp mark_greenlit onto the manifest + journal the decision. Left "
            "False, the block only DIGESTS the spec (nothing is stamped)."
        ),
    )
    response: str = Field(
        default="y",
        description=(
            "The human's answer to journal when confirm is set ('y' for a plain "
            "greenlight, or the nudge text that shaped the final spec). Ignored "
            "when confirm is False."
        ),
    )
    proposal: str | list[Any] | dict[str, Any] | None = Field(
        default=None,
        description=(
            "The LLM's drafted proposal over the spec brief, journaled verbatim "
            "alongside the response when confirm is set. Optional."
        ),
    )
    journal: bool = Field(
        default=True,
        description=(
            "When confirm is set, also append the greenlight to the campaign "
            "decision journal. Disable only to stamp the marker without a "
            "decision record (e.g. a re-stamp)."
        ),
    )


class CampaignWatchSpec(BaseModel):
    """Inputs to ``campaign-watch`` (async execution surface).

    Just the campaign id: the greenlit manifest is the complete contract (§4),
    so budget / stop / anomaly thresholds all default from it — watch reads,
    it never re-specifies them. Pure read; runs no tick.
    """

    model_config = ConfigDict(extra="forbid", title="campaign-watch input spec")

    campaign_id: str = Field(
        min_length=1,
        description="The running campaign to digest for a health / anomaly brief.",
    )


class CampaignCompleteSpec(BaseModel):
    """Inputs to ``campaign-complete`` (end).

    Just the campaign id: the completion brief is a code digest over the
    campaign's own durable state (manifest + sidecars + runtime-prior spend).
    """

    model_config = ConfigDict(extra="forbid", title="campaign-complete input spec")

    campaign_id: str = Field(
        min_length=1,
        description="The campaign to build the completion brief for.",
    )
