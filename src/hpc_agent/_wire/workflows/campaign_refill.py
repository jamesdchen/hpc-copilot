"""Pydantic models for the ``campaign-refill`` workflow primitive (RFC #362).

The refill ACTOR of the continuous-async campaign (``docs/design/campaign-async-refill.md``).
``campaign-advance`` (``meta/campaign/atoms/advance.py::_refill``) is the pure
AUTHORITY — it decides, per tick, whether a free pool slot with budget headroom
wants topping up (``decision=="refill"``, carrying ``refill_count``). It never
submits. ``campaign-refill`` is the side-effecting arm that consumes that
decision and, for each of ``refill_count`` slots, builds the next iteration's
``campaign-run`` spec and spawns it detached — re-homed onto the block-drive
architecture after the worker-removal wave deleted the old
``deterministic_resolver`` refill arm (the ``load_context`` comment cited it).

One-step-per-tick (NON-NEGOTIABLE): ``campaign-refill`` holds NO driver memory.
It recomputes the whole refill decision from journal state each tick via
``campaign-advance``; every submitted iteration writes its sidecar immediately
so ``campaign-status.in_flight`` rises and the NEXT tick's ``refill_count``
shrinks (crash-mid-tick self-corrects — no cursor, no new state file).

Standing consent: refill refuses an un-greenlit campaign. The greenlight is the
ONE human boundary of an async campaign (human-amplification design §4); the
per-iteration refills carry none — they are the autonomous execution the
greenlight authorized.

I/O contracts:

* Input: ``schemas/campaign_refill.input.json`` (from ``CampaignRefillSpec``).
* Output: ``schemas/campaign_refill.output.json`` (from ``CampaignRefillResult``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SubmittedIteration(BaseModel):
    """One refill slot that spawned a detached ``campaign-run`` child this tick."""

    model_config = ConfigDict(extra="forbid", title="campaign-refill submitted iteration")

    run_id: str = Field(description="The run_id resolve-submit-inputs computed for this iteration.")
    detached_pid: int | None = Field(
        default=None,
        description=(
            "The detached campaign-run worker's OS process id (informational — do "
            "NOT wait on it; the outcome arrives via the journal). None if the "
            "child replayed a recorded terminal instead of spawning."
        ),
    )
    stage_reached: str = Field(
        description="The campaign-run child's stage_reached (``detached`` on a fresh spawn).",
    )


class BlockedSlot(BaseModel):
    """A refill slot that stopped at a genuine escalation before it could submit.

    ``resolve-submit-inputs`` returned ``prior_run_found`` (a live prior matches
    this cmd_sha — only the human picks resume-vs-fresh) or
    ``needs_scaffold_interview`` (``.hpc/tasks.py`` absent). The refill loop
    BREAKS on the first blocked slot — continuing would ask more trials against
    an unresolved slot — so ``campaign-refill`` returns ``refill_blocked``.
    """

    model_config = ConfigDict(extra="forbid", title="campaign-refill blocked slot")

    run_id: str | None = Field(
        default=None,
        description="The computed run_id of the blocked slot, when resolve got that far; else null.",
    )
    stage: str = Field(
        description="resolve-submit-inputs stage that blocked (prior_run_found / "
        "needs_scaffold_interview).",
    )
    reason: str = Field(description="The resolve-submit-inputs reason the human must act on.")


class CampaignRefillSpec(BaseModel):
    """Inputs to ``campaign-refill`` (RFC #362).

    Just the campaign id: the greenlit manifest is the COMPLETE contract (mirror
    :class:`~hpc_agent._wire.workflows.campaign_blocks.CampaignWatchSpec`).
    ``async_refill`` / ``max_in_flight`` (the pool target K) / budget / stop all
    default from the manifest via ``campaign-advance`` — refill NEVER re-specifies
    K here, so the routing target (``campaign-watch``/``load-context``) and the
    refill target read the same authority.
    """

    model_config = ConfigDict(extra="forbid", title="campaign-refill input spec")

    campaign_id: str = Field(
        min_length=1,
        description="The greenlit async-refill campaign whose pool to top up this tick.",
    )


class CampaignRefillResult(BaseModel):
    """Shape of the ``data`` field on a ``campaign-refill`` envelope.

    ``stage_reached`` is the deterministic dispatch over the advance decision +
    per-slot resolve outcomes; ``needs_decision`` is set only on
    ``refill_blocked`` (a slot hit a resume-vs-fresh / scaffold escalation a
    human must resolve). The clean ``refilled`` / ``no_refill_needed`` terminals
    end the chain — the next cron/loop tick re-enters via ``campaign-watch``
    (one-step-per-tick).
    """

    model_config = ConfigDict(extra="forbid", title="campaign-refill output data")

    stage_reached: Literal[
        "refilled",
        "no_refill_needed",
        "refill_blocked",
    ] = Field(description="Which refill outcome this tick reached.")
    needs_decision: bool = Field(
        description=(
            "True only for refill_blocked (a slot hit a live-prior / scaffold "
            "escalation the human must resolve); False for refilled and the "
            "no_refill_needed typed no-op."
        ),
    )
    reason: str = Field(description="Human-readable summary of the tick's outcome.")
    campaign_id: str | None = Field(
        default=None,
        description="The campaign this refill tick operated on.",
    )
    decision: str | None = Field(
        default=None,
        description=(
            "The campaign-advance decision this tick (refill / wait_in_flight / "
            "stop_* / continue) — the authoritative source refill consumed."
        ),
    )
    refill_count: int | None = Field(
        default=None,
        description=(
            "The pool-slots-to-fill count advance requested this tick "
            "(max(0, min(K - in_flight, remaining_max_jobs))); 0 on no_refill_needed."
        ),
    )
    submitted: list[SubmittedIteration] = Field(
        default_factory=list,
        description="One row per spawned detached campaign-run child (run_id + pid + stage).",
    )
    blocked: list[BlockedSlot] = Field(
        default_factory=list,
        description="Slots that stopped at a resume-vs-fresh / scaffold escalation before submit.",
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Always null in practice: the chain ENDS at campaign-refill — the next "
            "cron/loop tick re-enters via campaign-watch (one-step-per-tick). The "
            "field is DECLARED so the MCP curated catalog derives campaign-refill "
            "as a block (a block is any verb whose Result declares next_block)."
        ),
    )
    active_env_overrides: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Every framework-relevant HPC_* environment variable currently exported "
            "in THIS process's environment, verbatim (B15 disclosure; mirror "
            "CampaignRunResult). Refill spawns cluster-submitting children, so a "
            "stray transport override reshapes every submit — echoing it makes the "
            "env-vs-record drift visible. Empty when no HPC_* variable is set."
        ),
    )
