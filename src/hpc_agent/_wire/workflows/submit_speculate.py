"""Pydantic models for ``submit-speculate`` — the speculative canary (design §3).

The block-parallelism latency opportunity (``docs/design/human-amplification-blocks.md``
§3, "Speculative canary"): while the human reviews S1's decision brief, run the
canary under the *recommended* defaults. On a plain ``y`` the spec is unchanged →
S2 finds the canary already validated-fresh and skips it; on a nudge that changes
the spec the ``cmd_sha`` moves → the stale canary's cache entry no longer matches
and S2 re-canaries. Cheap by design (a single-task array), so mis-speculation is
bounded.

This is the ONE sanctioned auto-apply of the S1 recommendations: the human is
concurrently reviewing the brief, and the canary is a bounded, self-cleaning
probe (§3 pre-greenlight cluster-touch policy). Nothing beyond the canary ever
enters the queue before a greenlight.

Interleaving rules honored (§3, decided 2026-07-03):

* **Budget = 1 per pending brief.** A speculative canary whose ``(cmd_sha,
  version)`` is already validated-fresh is refused (no-op result) — the TTL
  cache IS the dedup; no extra in-flight bookkeeping is built.
* **Never cancels.** ``submit-speculate`` only submits + verifies a canary; a
  superseding nudge's stale canary drains naturally (no kill path).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec


class SubmitSpeculateSpec(BaseModel):
    """Inputs to ``submit-speculate`` (the speculative canary).

    ``submit`` is the SAME ``submit-and-verify`` spec ``submit-s2`` would run, with
    the S1 recommendations already applied into its resolved fields — the
    sanctioned auto-apply for this bounded, pre-greenlight canary. The verb runs
    it with ``stop_after_canary`` so only the 1-task canary enters the queue; the
    main array NEVER launches before a greenlight.
    """

    model_config = ConfigDict(extra="forbid", title="submit-speculate input spec")

    submit: SubmitAndVerifySpec = Field(
        description=(
            "The submit-and-verify spec (S1 recommendations applied). Must have "
            "submit.canary=True. Run with stop_after_canary — only the canary fires."
        ),
    )
    detach: bool = Field(
        default=True,
        description=(
            "Detach-by-contract (design §3): default ON — never-stall is the norm. "
            "The budget/dedup check (a validated-fresh canary → no-op) runs "
            "synchronously first; when a fresh canary WOULD fire and detach is True, "
            "submit-speculate spawns a durable detached worker to own the canary "
            "poll and returns immediately with a {started, watch: journal, "
            "detached_pid} handle, so speculation never holds the chat while the "
            "human reviews the S1 brief. Set False to run the canary synchronously "
            "(tests / CI)."
        ),
    )


class SubmitSpeculateResult(BaseModel):
    """Result of ``submit-speculate``.

    ``speculated`` is False on the budget-dedup no-op path (a validated-fresh
    canary already exists for this ``(cmd_sha, version)`` — S2 will reuse it) and
    True when a fresh speculative canary fired. ``verified`` mirrors the canary
    verification outcome. This is NOT a sequenced block, so it carries no
    ``next_block`` — S1's brief owns the next-block suggestion.
    """

    model_config = ConfigDict(extra="forbid", title="submit-speculate output data")

    run_id: RunIdStrict = Field(description="The (canary) run this speculation operated on.")
    speculated: bool = Field(
        description=(
            "True when a fresh speculative canary fired; False on the budget no-op "
            "(a validated-fresh canary already exists — S2 will reuse it)."
        ),
    )
    verified: bool = Field(
        description="True when the speculative canary verified (or a fresh one is already validated)."
    )
    reason: str = Field(default="", description="One-line summary of the outcome.")
    canary_run_id: str | None = Field(
        default=None, description="The speculative canary's run_id, when one fired."
    )
    canary_job_ids: list[str] | None = Field(
        default=None, description="The speculative canary's scheduler ids, when one fired."
    )
    failure_kind: str | None = Field(
        default=None, description="The verify failure kind when the speculative canary failed."
    )
    started: bool = Field(
        default=False,
        description=(
            "Detach-by-contract handle (design §3): True when the speculative canary "
            "was handed to a durable detached worker and this call returned "
            "immediately. ``verified`` is not yet known (the poll runs in the "
            "worker) — read the outcome from the journal. False on the synchronous "
            "path and on the budget no-op."
        ),
    )
    watch: str | None = Field(
        default=None,
        description=(
            'How to learn the detached canary\'s outcome — ``"journal"`` when '
            "``started`` is True. None otherwise."
        ),
    )
    detached_pid: int | None = Field(
        default=None,
        description="The detached worker's OS process id (informational). None otherwise.",
    )
