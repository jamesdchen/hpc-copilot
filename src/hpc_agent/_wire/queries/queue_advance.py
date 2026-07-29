"""Pydantic models for the ``queue-advance`` query (run queue, Phase 1).

Wire surface over :mod:`hpc_agent.ops.queue.advance` — the placement
AUTHORITY, copying the proven ``campaign-advance`` / ``campaign-refill``
authority/actor split (§2).

**R3 — pure.** Reads only, returns a decision, writes nothing. The empty
``side_effects`` list on the primitive is that promise mechanized; this result
is the whole of what the verb produces. Deterministic and unit-testable is not
a nicety here: placement is disclosed inside the human's y (§3), so a decision
that could not be recomputed from the same inputs would make the y unfalsifiable.

**R4 — every held-back item carries a DISCLOSED REASON.** No item is ever
dropped and no cluster is ever guessed. If nothing qualifies, the item STAYS
queued and ``held`` says why, per item and per cluster, so a brief can render
"3 items waiting on gpu capacity" from data instead of from prose.

**R5 — an explicit pin wins over policy**, always, and the placement says so
(``pinned``).

**R6 / §7 R3 — no gating on inferred scheduler capacity.** Slurm/SGE already
run the authoritative resource queue and their state is the one thing we
cannot predict from outside; two queues in series, each doing only what it
can. What remains OURS is cross-cluster choice (each scheduler sees only its
own queue), courtesy caps (lab etiquette — centers penalize queue-flooding
accounts), and hard-constraint mismatch. There is deliberately no
``no_capacity`` hold reason: S11 confirmed ``max_concurrent_jobs`` is
per-submission-plan wave grouping, NOT a cross-run account cap, so citing it
as one would be inventing a second meaning for a field that already has one.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    CampaignId,
    QueueItemId,
    RunIdStrict,
    Scheduler,
    SshTarget,
)

#: Why an item was held back, as a CLOSED vocabulary so a brief can count by
#: cause and a test can assert on the cause rather than on prose. Held-back
#: reasons are DATA in this result, never envelope errors — R4 makes them a
#: normal, expected outcome of a healthy queue, not a failure of the verb.
QueueHoldReason = Literal[
    # No clusters.yaml entries at all — the config is empty or unreadable.
    "no_clusters_configured",
    # The item pins a cluster key that clusters.yaml does not define (a typo, or
    # a cluster that was retired). NEVER silently re-placed: R5 says the pin is a
    # decision, so overriding it would be the tool overruling the operator.
    "cluster_pin_unknown",
    # Every candidate cluster failed at least one hard constraint from the
    # item's typed resource asks (gpu / gpu_type / walltime / cores).
    "no_cluster_matches_constraints",
    # A courtesy cap we enforce ourselves would be exceeded (lab etiquette /
    # anti-throttle policy, the connection-storm lineage) — explicitly NOT an
    # inference about scheduler headroom (R6).
    "courtesy_cap_reached",
    # The item lacks facts placement needs (no resolvable spec / run_name), so
    # no cluster can be evaluated for it honestly.
    "item_unresolved",
    # This call's ``max_placements`` bound was reached first. The item is fine;
    # the CALLER limited the batch. Distinguished so nobody reads a bound as a
    # constraint failure.
    "batch_limit_reached",
]


class QueueAdvanceSpec(BaseModel):
    """Input spec for ``queue-advance`` — scope and bound, never a placement."""

    model_config = ConfigDict(extra="forbid", title="queue-advance input spec")

    campaign_base: CampaignId | None = Field(
        default=None,
        description=(
            "Restrict the decision to items enqueued under this logical campaign "
            "base. Null (default) considers every queued item — the fleet read a "
            "drain pass wants."
        ),
    )
    max_placements: int = Field(
        default=1,
        ge=1,
        le=50,
        description=(
            "Most placements to decide in this call. Defaults to ONE because "
            "placement is disclosed inside a y and a human signs one decision at "
            "a time; a larger batch is for the consented/unattended tier. "
            "Deciding is not actuating (R3), so the bound stays caller-set "
            "here — the tier is enforced at the ACTOR, where "
            "``queue-dispatch`` refuses a >1 batch that neither declares "
            "``tier='unattended'`` nor enumerates its ``item_ids``. Items "
            "beyond the bound are reported as held with reason "
            "'batch_limit_reached', never dropped (R4)."
        ),
    )
    clusters: list[str] | None = Field(
        default=None,
        description=(
            "Optional restriction of the candidate set to these clusters.yaml "
            "keys. Null (default) considers every configured cluster. This "
            "NARROWS the candidates; it can never place an item on a cluster the "
            "hard constraints reject, and it never overrides an item's pin (R5)."
        ),
    )
    now: str | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 UTC 'now' override for deterministic testing (the "
            "doctor / attention-queue precedent). Sets ``computed_at``; never an "
            "agent-facing knob for reshaping the decision."
        ),
    )


class QueueClusterVerdict(BaseModel):
    """Why ONE cluster was or was not eligible for ONE item.

    The per-cluster evidence behind a hold. R4 says an item never disappears
    without a reason; a reason that says only "nothing matched" is a reason the
    human cannot act on, so the decision carries its work.
    """

    model_config = ConfigDict(extra="forbid", title="queue-advance cluster verdict")

    cluster: str = Field(description="The clusters.yaml top-level key evaluated.")
    eligible: bool = Field(description="True when this cluster satisfied every hard constraint.")
    reason: str = Field(
        description=(
            "The specific verdict — which constraint failed and against which "
            "configured value (e.g. 'walltime_sec 172800 > constraints.max_walltime "
            "3:00:00'). Names WHICH walltime ceiling it compared, because "
            "clusters.yaml carries two that disagree by 8-16x (S11)."
        ),
    )
    occupied: int = Field(
        default=0,
        description=(
            "Pool slots this cluster's campaign id already occupies, from the ONE "
            "shared predicate ``state/queue_occupancy.occupied_slots`` (R9). The "
            "least-loaded tie-break input, and a disclosed number rather than an "
            "inferred one — it counts OUR committed work, not the scheduler's queue."
        ),
    )


class QueuePlacement(BaseModel):
    """One decided placement: this item, that cluster, and the disclosed why.

    This is what §3 means by placement living INSIDE the y. The greenlight
    binds to a spec and the spec names the cluster, so the brief must be able
    to say "next: rv_sweep item 4 -> hoffman2 (carc at 2/2, item needs gpu)" —
    every token of which comes from this object.
    """

    model_config = ConfigDict(extra="forbid", title="queue-advance placement")

    item_id: QueueItemId = Field(description="The intake item this decision is about.")
    run_id: RunIdStrict | None = Field(
        default=None,
        description=(
            "The item's COMPUTED run id when known (§10.S2) — the key a dispatcher "
            "claims on, and the key the occupancy predicate collapses onto."
        ),
    )
    cluster: str = Field(
        description="The chosen clusters.yaml top-level key. Never guessed: absent a qualifying cluster the item is HELD instead.",
    )
    campaign_id: CampaignId | None = Field(
        default=None,
        description=(
            "The concrete ``<base>_<clusterkey>`` campaign id this placement "
            "implies, composed from the item's campaign_base and the chosen "
            "cluster key (campaign-multi-cluster.md §2). Null for an open-loop "
            "item. Composed, never parsed — a base routinely contains "
            "underscores, so splitting one back apart is a hazard."
        ),
    )
    scheduler: Scheduler | None = Field(
        default=None,
        description=(
            "The chosen cluster's scheduler family, disclosed because it changes "
            "what the run can promise (S11: SGE cannot enforce the canary "
            "success-gate). Validated against the live backend registry."
        ),
    )
    ssh_target: SshTarget | None = Field(
        default=None,
        description=(
            "The chosen cluster's ``user@host`` as resolved from config AT "
            "DECISION TIME, for disclosure only. A run's recorded ssh_target is "
            "stale-by-design and re-resolved at use; nothing downstream should "
            "consume this field as the live target."
        ),
    )
    pinned: bool = Field(
        default=False,
        description=(
            "True when an explicit cluster pin decided this, not policy (R5). The "
            "brief must not present an operator's own choice back to them as the "
            "tool's recommendation."
        ),
    )
    reason: str = Field(
        description=(
            "The disclosed WHY, in one line, naming the deciding factor and the "
            "runner-up. This string is what the y is taken against, so it must "
            "state the actual rule that fired (pin / hard constraint elimination "
            "/ least-loaded / alphabetical tie-break), never a summary of it."
        ),
    )
    considered: list[QueueClusterVerdict] = Field(
        default_factory=list,
        description=(
            "Every cluster evaluated for this item, with its verdict — the "
            "evidence behind ``reason``, so a human can check the choice rather "
            "than trust it."
        ),
    )


class QueueHoldback(BaseModel):
    """One item that stays QUEUED, with the reason stated (R4).

    Never dropped, never guessed at. A held item is a normal outcome — under
    §7 R3 the queue's 'queued' state is almost entirely PRE-GATE (awaiting a y,
    consent, or placement), not awaiting capacity.
    """

    model_config = ConfigDict(extra="forbid", title="queue-advance holdback")

    item_id: QueueItemId = Field(description="The intake item held back.")
    reason_code: QueueHoldReason = Field(
        description="The closed-vocabulary cause, so briefs can COUNT by cause ('3 items waiting on gpu capacity').",
    )
    reason: str = Field(
        description="The human-readable statement of the same cause, specific enough to act on.",
    )
    considered: list[QueueClusterVerdict] = Field(
        default_factory=list,
        description="Every cluster evaluated and why each was rejected — empty only when no cluster could be evaluated at all.",
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured particulars behind ``reason`` (the unknown pin and its "
            "near-miss suggestions, the cap and its current value, the asks that "
            "found no host). Machine-readable so a later tier can act on the hold "
            "without re-parsing prose."
        ),
    )


class QueueAdvanceResult(BaseModel):
    """The DECISION — placements, holds, and the disclosure that carries them."""

    model_config = ConfigDict(extra="forbid", title="queue-advance output data")

    computed_at: str = Field(
        description="The single instant the decision was computed against (ISO-8601 UTC).",
    )
    decision: Literal["place", "hold", "empty"] = Field(
        description=(
            "The call's top-level outcome: 'place' when at least one placement "
            "was decided, 'hold' when items are queued but none could be placed, "
            "'empty' when nothing was queued at all. Three states, not two, "
            "because 'nothing to do' and 'something to do that I refused to "
            "guess at' are opposite situations for the human."
        ),
    )
    placements: list[QueuePlacement] = Field(
        default_factory=list,
        description=(
            "The decided placements, at most ``max_placements``, in the order "
            "they should be dispatched. A DECISION, not an action: this verb "
            "writes nothing (R3) — the actor consumes it."
        ),
    )
    held: list[QueueHoldback] = Field(
        default_factory=list,
        description="Every item that stays queued, each with its disclosed reason (R4). Never elided.",
    )
    held_counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "reason_code -> count over ``held``. The pre-aggregated numbers a "
            "morning brief renders ('3 items waiting on gpu capacity') so no "
            "consumer has to re-derive them and risk a different total."
        ),
    )
    queued_total: int = Field(
        default=0,
        description="How many items were in state 'queued' and in scope when the decision was computed.",
    )
    considered_clusters: list[str] = Field(
        default_factory=list,
        description=(
            "The candidate cluster keys this call evaluated, after any ``clusters`` "
            "restriction. Disclosed because an empty or unexpectedly narrow "
            "candidate set is the likeliest cause of a surprising hold."
        ),
    )
    occupancy: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "campaign_id -> occupied pool slots at decision time, from the ONE "
            "shared predicate ``state/queue_occupancy.occupied_slots`` (R9). The "
            "least-loaded input, published so the decision's arithmetic is "
            "checkable and so campaign-advance and queue-advance cannot come to "
            "different totals."
        ),
    )
    brief: str = Field(
        default="",
        description=(
            "The deterministic one-paragraph disclosure the session relays "
            "VERBATIM into the y — code-computed from the fields above, never "
            "authored. §3: the human's y covers the placement, so the placement "
            "has to be in front of them in the same breath as the question."
        ),
    )
