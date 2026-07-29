"""Pydantic models for the ``queue-dispatch`` workflow (run queue, Phase 2).

Wire surface over :mod:`hpc_agent.ops.queue.dispatch` — the ACTOR half of the
authority/actor split ``queue-advance`` opened (§2, copied from
``campaign-advance`` / ``campaign-refill``). ``queue-advance`` decides and
writes nothing (R3); ``queue-dispatch`` consumes those placements, records each
one on the intake ledger, and starts the item's NORMAL run lifecycle.

Four decisions shape this surface more than anything else
(``docs/plans/run-queue-placement-2026-07-28.md`` §10):

* **It COMPOSES; it does not submit.** There is no second submit path here, no
  gate of its own, and no consent machinery of its own. Dispatch starts the
  already-gated lifecycle and the lifecycle refuses or proceeds on its own
  rules — the greenlight / standing consent bind at the cluster boundary
  exactly where they bind today. That is why this spec carries no consent,
  no greenlight, and no submit knob: a dispatch-side gate would be a SECOND
  place the same question is answered, and the two would drift.
* **The claim is the shipped lease, not a new one** (§10.S2). Run ids are
  COMPUTED — ``"<run_name>-<cmd_sha[:8]>"``, pure and deterministic — so two
  dispatchers racing one item derive the SAME id and the detached single-lease
  guard (host + pid + create_time, under a flock) arbitrates. The loser is
  reported as ``claim_held``, which is a normal outcome of a healthy fleet.
* **ADOPT rather than resubmit** (§10.S2.5, amended). The submit-once
  ``submitting`` RunRecord is the durable "this item is already in its
  dispatch→id window" fact, so the pre-submit check is a local runs-store read
  keyed on the computed run_id. It is deliberately NOT a scheduler probe by job
  name: ``job_name`` is capped at 15 characters on SGE and consumed
  byte-for-byte by log-path derivation and canary naming, which is the reason
  the shipped backend contract already refuses to carry identity there. Every
  adopt is disclosed, with the status that caused it.
* **No new intake state** (§8 S10 / R1). Intake stores ``{queued, placed}``
  and nothing wider; "dispatched" stays a PROJECTION over the run stores, which
  ``queue-status`` already computes. So the only ledger write on this path is
  the placement transition, and the result below reports the dispatch as a
  fact about the RUN stores rather than as a state it invented.

R4/R9's disclosure rule carries through unchanged: no item is ever dropped.
Every item this call touched is in exactly one of ``dispatched``, ``held``
(queue-advance's own holdbacks, relayed verbatim) or ``refused``, each with a
reason a human can act on.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import (
    CampaignId,
    CmdSha,
    QueueItemId,
    RunIdStrict,
)
from hpc_agent._wire.queries.queue_advance import QueueHoldback

#: Why a PLACED item did not enter its lifecycle on this call, as a closed
#: vocabulary so a brief can count by cause and a test can assert on the cause
#: rather than on prose. Disjoint from ``QueueHoldReason`` on purpose: that
#: vocabulary is about placement (the authority could not choose a cluster),
#: this one is about actuation (a cluster was chosen and the actor could not
#: start). Collapsing them would hide which half of the organ said no.
QueueDispatchRefusal = Literal[
    # Another dispatcher holds the claim for this run_id (the detached lease) or
    # this campaign's dispatch lock. Not an error: the peer is doing the work.
    "claim_held",
    # The item carries no resolvable identity — no run_name, or a spec the
    # resolver could not turn into a run_id/cmd_sha — so nothing can be claimed
    # or adopted for it. The placement stands; the actuation cannot.
    "item_unresolved",
    # resolve-submit-inputs stopped short of ``resolved`` (a live prior run
    # matches this cmd_sha and only a human picks resume-vs-fresh, or the
    # scaffold interview is missing). A decision, escalated rather than guessed.
    "resolve_blocked",
    # The placed cluster could not be resolved to a live transport at use time —
    # the key left clusters.yaml, or its ssh target is unresolvable. Placement
    # discloses a target for the human; the actor always re-resolves, and this
    # is what it means when the re-resolve fails.
    "cluster_unresolvable",
    # The gated lifecycle refused: no greenlight, no standing consent, a hard
    # cap. Surfaced verbatim in ``reason`` — dispatch neither re-asks the
    # question nor overrides the answer.
    "gate_refused",
    # The lifecycle was started and failed before it could report a run. The
    # item stays placed and the failure is named; a retry is a new dispatch.
    "lifecycle_failed",
]


class QueueDispatchSpec(BaseModel):
    """Inputs to ``queue-dispatch`` — scope and bound, never a placement."""

    model_config = ConfigDict(extra="forbid", title="queue-dispatch input spec")

    campaign_base: CampaignId | None = Field(
        default=None,
        description=(
            "Restrict this call to items enqueued under this logical campaign "
            "base, forwarded to ``queue-advance`` unchanged. Null considers every "
            "queued item — the fleet-wide drain read."
        ),
    )
    item_ids: list[QueueItemId] | None = Field(
        default=None,
        description=(
            "Act on ONLY these items. The refill path's field (§10.S3): a slot "
            "enqueues one item and then dispatches THAT item in the same "
            "critical section, so it must be able to name it rather than trust "
            "arrival order to hand it back. This NARROWS what the call acts on; "
            "it never places anything — ``queue-advance`` remains the sole "
            "authority, so an item named here that advance HELD is reported held, "
            "with advance's own reason, and is not dispatched. Null (default) "
            "acts on whatever advance placed, in the order it placed them."
        ),
    )
    max_dispatches: int = Field(
        default=1,
        ge=1,
        le=50,
        description=(
            "Most items to start in this call. Defaults to ONE for the same "
            "reason ``queue-advance.max_placements`` does: a human signs one "
            "decision at a time, and a larger batch belongs to the "
            "consented/unattended tier — so a value above 1 must either declare "
            "that tier (``tier='unattended'``) or enumerate the batch item by "
            "item (``item_ids``); the model refuses a silent raise. Items "
            "beyond the bound stay queued and are reported held with advance's "
            "``batch_limit_reached`` — never dropped (R4)."
        ),
    )
    tier: Literal["interactive", "unattended"] = Field(
        default="interactive",
        description=(
            "Which tier this call runs under. 'interactive' (default): a human "
            "is signing decisions one y at a time, so ``max_dispatches`` stays "
            "at 1. 'unattended': the CALLER declares the standing-consent tier "
            "— a machine-fired tick (the retirement wake edge, a scheduled "
            "drain) where no y is being taken, items a live standing consent "
            "covers auto-advance at their gates and the rest PARK for their own "
            "y — which is what makes a >1 batch honest: it removes only the "
            "one-per-y throttle. A DECLARATION, deliberately not verified here: "
            "consent binds per item at the cluster boundary inside the "
            "lifecycle this verb composes, and a dispatch-side consent read "
            "would be a second seat answering the same question (D1). A falsely "
            "declared tier therefore bypasses nothing — it buys disclosed "
            "gate_refused/parked rows. The batch's basis is echoed on the "
            "result (``batch_allowed_by``), never silent."
        ),
    )
    clusters: list[str] | None = Field(
        default=None,
        description=(
            "Optional restriction of the candidate cluster set, forwarded to "
            "``queue-advance``. NARROWS the candidates; never overrides an item's "
            "pin (R5) and never places an item the hard constraints reject."
        ),
    )
    now: str | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 UTC 'now' override for deterministic testing (the "
            "doctor / attention-queue precedent). Sets ``computed_at`` and is "
            "forwarded to ``queue-advance``; never an agent-facing knob for "
            "reshaping what gets dispatched."
        ),
    )

    @model_validator(mode="after")
    def _batch_declares_its_basis(self) -> QueueDispatchSpec:
        """Refuse a >1 batch that neither declares the unattended tier nor names items.

        The interactive default is ONE because placement is disclosed inside a
        human's y and a human signs one decision at a time; the doctrine
        reserves a larger batch for the consented/unattended tier. Enforcing
        that at the wire is what keeps the reservation real: no agent or config
        can raise the interactive bound SILENTLY, because the batch's basis —
        the declared tier, or the caller's own enumerated ``item_ids`` (the
        stranded-recovery shape, where naming each item IS the signature) — is
        part of the spec and echoed on the result. A disclosure rule, not a
        gate: every dispatched item still meets its own cluster-boundary gates
        inside the lifecycle (D1).
        """
        if self.max_dispatches > 1 and self.tier != "unattended" and self.item_ids is None:
            raise ValueError(
                f"max_dispatches={self.max_dispatches} exceeds the interactive "
                "one-decision-per-y bound; a larger batch must declare the "
                "consented tier (tier='unattended') or enumerate its items "
                "(item_ids) — it is never raised silently"
            )
        return self


class DispatchedItem(BaseModel):
    """One item whose run lifecycle this call started — or found already started.

    ``outcome`` is the whole point of the row. Dispatch is idempotent by
    ADOPTION, not by suppression: an item whose run already exists is a
    SUCCESS, reported as such, with the evidence that made it one. A dispatcher
    that silently skipped it would be indistinguishable from one that crashed
    before starting anything.
    """

    model_config = ConfigDict(extra="forbid", title="queue-dispatch dispatched item")

    item_id: QueueItemId = Field(description="The intake item that entered its lifecycle.")
    run_id: RunIdStrict = Field(
        description=(
            "The COMPUTED run id this dispatch claimed (§10.S2). Required here, "
            "unlike on a placement: an item with no derivable id cannot be "
            "claimed, adopted or started at all, so it is a refusal "
            "(``item_unresolved``) rather than a dispatch."
        ),
    )
    cmd_sha: CmdSha | None = Field(
        default=None,
        description=(
            "The parameter identity behind ``run_id`` — its pre-image, when the "
            "ledger or the resolve supplied one. Carried so an adopt can state "
            "which parameters the existing run committed to rather than implying "
            "agreement from eight hex characters."
        ),
    )
    cluster: str = Field(
        description=(
            "The clusters.yaml key the item was placed on and started against. "
            "Transport was RE-RESOLVED from this key at use time; the placement's "
            "own ssh_target is disclosure only and stale by design."
        ),
    )
    campaign_id: CampaignId | None = Field(
        default=None,
        description=(
            "The concrete ``<base>_<clusterkey>`` this dispatch counts against — "
            "the key of the per-campaign dispatch lock it held and of the pool "
            "slot it now occupies. Null for an open-loop item."
        ),
    )
    outcome: Literal["started", "adopted"] = Field(
        description=(
            "'started' — this call spawned the lifecycle. 'adopted' — a run for "
            "this run_id already existed and this call took it as the item's "
            "dispatch instead of submitting a second one (§10.S2.5). The "
            "submit-once organ makes the difference observable rather than "
            "guessed: a live ``submitting`` record IS the in-flight dispatch."
        ),
    )
    adopted_status: str | None = Field(
        default=None,
        description=(
            "On an adopt, the existing RunRecord's status — 'submitting' (a "
            "concurrent dispatch is inside its dispatch→id window; reconcile "
            "recovery owns it), or 'in_flight'/'complete' (the work is already "
            "done or running). Null on 'started'. The evidence behind the adopt, "
            "so nobody has to re-read the journal to check it."
        ),
    )
    placed: bool = Field(
        default=False,
        description=(
            "True when THIS call appended the placement transition; False when "
            "the ledger already carried it (a retry, or a peer that got there "
            "first). The transition's append token is derived from the item id, "
            "so a replay writes nothing — this is the honest signal of which of "
            "the two happened."
        ),
    )
    detached_pid: int | None = Field(
        default=None,
        description=(
            "The detached lifecycle worker's OS process id, informational — do "
            "NOT wait on it; the outcome arrives via the journal. Null on an "
            "adopt and on a replayed terminal."
        ),
    )
    stage_reached: str | None = Field(
        default=None,
        description="The composed lifecycle verb's own stage_reached ('detached' on a fresh spawn).",
    )
    reason: str = Field(
        description=(
            "The disclosed WHY, in one line: what was started or adopted and what "
            "made that the right call. R4 applies to successes too — an adopt "
            "that did not say why it adopted is indistinguishable from a "
            "dispatch that silently did nothing."
        ),
    )


class DispatchRefusal(BaseModel):
    """One PLACED item that did not enter its lifecycle, with the cause stated.

    The actor's half of R4. ``queue-advance``'s holdbacks say "I could not
    choose a cluster"; these say "a cluster was chosen and I could not start".
    Both are normal outcomes carried as DATA, never as envelope errors — a
    claim held by a peer is a healthy fleet, not a failed call.
    """

    model_config = ConfigDict(extra="forbid", title="queue-dispatch refusal")

    item_id: QueueItemId = Field(description="The intake item that stayed un-started.")
    run_id: RunIdStrict | None = Field(
        default=None,
        description="The computed run id, when one was derivable; null for an unresolved item.",
    )
    cluster: str | None = Field(
        default=None,
        description="The cluster the item was placed on, when placement got that far.",
    )
    campaign_id: CampaignId | None = Field(
        default=None,
        description="The cid this item would have occupied a slot in; null for an open-loop item.",
    )
    reason_code: QueueDispatchRefusal = Field(
        description="The closed-vocabulary cause, so briefs can COUNT by cause and tests can assert on it.",
    )
    reason: str = Field(
        description=(
            "The human-readable statement of the same cause, specific enough to "
            "act on — the peer's pid for a held claim, the resolver's own "
            "escalation text for a blocked resolve, the gate's refusal verbatim."
        ),
    )
    placed: bool = Field(
        default=False,
        description=(
            "Whether the placement transition was already on the ledger when the "
            "refusal happened. Load-bearing for the operator: a placed-but-"
            "un-started item has left ``queue-advance``'s scope (advance reads "
            "only queued items), so it will not be re-decided and "
            "``queue-status`` is the surface that must show it."
        ),
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured particulars behind ``reason`` (the holder pid, the "
            "resolver stage, the missing cluster key and its near misses), so a "
            "later tier can act on the refusal without re-parsing prose."
        ),
    )


class QueueDispatchResult(BaseModel):
    """What this call started, what it refused, and what advance held back."""

    model_config = ConfigDict(extra="forbid", title="queue-dispatch output data")

    computed_at: str = Field(
        description="The single instant this call was computed against (ISO-8601 UTC).",
    )
    stage_reached: Literal["dispatched", "nothing_to_dispatch", "dispatch_refused"] = Field(
        description=(
            "The call's outcome: 'dispatched' when at least one item entered its "
            "lifecycle (started OR adopted), 'nothing_to_dispatch' when there was "
            "nothing placed to act on, 'dispatch_refused' when every placed item "
            "was refused. Three states, not two, because 'nothing to do' and "
            "'work I could not start' are opposite situations for the human."
        ),
    )
    needs_decision: bool = Field(
        default=False,
        description=(
            "True only when a refusal is one a HUMAN must resolve — a blocked "
            "resolve (resume-vs-fresh, missing scaffold) or a refused gate. A "
            "claim held by a peer is explicitly NOT one: nobody needs to decide "
            "anything, the other process is already doing the work."
        ),
    )
    reason: str = Field(
        description="One-line summary of the tick's outcome, code-computed from the rows below.",
    )
    dispatched: list[DispatchedItem] = Field(
        default_factory=list,
        description="One row per item that entered its lifecycle this call, started or adopted.",
    )
    refused: list[DispatchRefusal] = Field(
        default_factory=list,
        description="One row per PLACED item that did not start, each with its disclosed cause (R4).",
    )
    held: list[QueueHoldback] = Field(
        default_factory=list,
        description=(
            "``queue-advance``'s own holdbacks, relayed VERBATIM — items that "
            "were never placed, so the actor had nothing to act on. Passed "
            "through rather than restated so the authority's reason is the one "
            "the human reads (R4)."
        ),
    )
    refused_counts: dict[str, int] = Field(
        default_factory=dict,
        description="reason_code -> count over ``refused``. Pre-aggregated so no consumer re-derives a different total.",
    )
    held_counts: dict[str, int] = Field(
        default_factory=dict,
        description="``queue-advance``'s reason_code -> count over ``held``, relayed unchanged.",
    )
    batch_allowed_by: Literal["unattended_tier", "item_ids"] | None = Field(
        default=None,
        description=(
            "Why a ``max_dispatches`` > 1 bound was accepted, or null for a "
            "call at the interactive one-per-y default: 'unattended_tier' — the "
            "caller declared the standing-consent tier; 'item_ids' — the caller "
            "enumerated the batch item by item. The R4 disclosure for the "
            "throttle, never a gate outcome: batching exempts no item from its "
            "own cluster-boundary gates (D1), it only removes the artificial "
            "one-start-per-tick bound."
        ),
    )
    placements_considered: int = Field(
        default=0,
        description=(
            "How many placements ``queue-advance`` returned for this call, before "
            "any ``item_ids`` narrowing. The denominator behind the rows above, "
            "so a call that acted on one of five placements says so."
        ),
    )
    occupancy: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "campaign_id -> occupied pool slots as the ONE shared predicate "
            "(``state/queue_occupancy.occupied_slots``, R9) saw them at decision "
            "time, relayed from ``queue-advance``. Published so the arithmetic "
            "behind a placement stays checkable after the fact."
        ),
    )
    maintenance: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "What the queue's JANITOR did on this tick, as data: "
            "``{dropped_items, dropped_records, kept_records, pruned_runs, "
            "protected_runs}`` and ``error`` when a leg failed "
            "(``ops/queue/maintenance.groom_queue_stores``). The dispatcher is "
            "the queue's only WRITE authority, so it is where settled intake "
            "records are compacted away and unreferenced terminal RunRecords are "
            "pruned — never on a read path, and never on a tick that dispatched "
            "nothing (that pass is the one §7's relaunch-cheapness invariant is "
            "written about). Empty when no grooming ran. Disclosed rather than "
            "silent: a durable store that shrank must say by how much."
        ),
    )
    brief: str = Field(
        default="",
        description=(
            "The deterministic one-paragraph disclosure the session relays "
            "VERBATIM — code-computed from the fields above, never authored. "
            "Placement lives inside the human's y (§3), so what was actually "
            "started, adopted, refused and held has to be renderable in one "
            "breath."
        ),
    )
    active_env_overrides: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Every framework-relevant ``HPC_*`` environment variable exported in "
            "THIS process, verbatim (B15 disclosure; mirrors campaign-refill). "
            "Dispatch starts cluster-submitting children, so a stray transport "
            "override reshapes every one of them — echoing it makes env-vs-record "
            "drift visible. Empty when no ``HPC_*`` variable is set."
        ),
    )
