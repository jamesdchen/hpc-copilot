"""Pydantic models for the ``queue-status`` query (run queue, Phase 1).

Wire surface over :mod:`hpc_agent.ops.queue.status`. ``queue-status`` is the
JOIN the whole queue design rests on:

    queue-status = intake items ⋈ projections over the stores that already exist

**R1 (§7 R1, §8 S10).** The ledger is an INDEX, not a second journal. It
stores arrival facts and ``{queued, placed}``; every other fact on an item
here — dispatched, parked, greenlit-but-unadvanced, in flight, terminal — is
RECOMPUTED at read time from RunRecords (``state/index.py``), pending-decision
markers (``state/journal.py``) and the decision journal
(``state/decision_journal.py``). A ledger that COPIED park state would drift
from the journal the first time a y landed; a ledger that projects it cannot.
Nothing on ``QueueStatusItem`` below is read back from intake except the
fields explicitly documented as ledger facts.

**R7 (§8 S12).** Greenlight questions route through
``state/decision_journal.py::is_committed_greenlight_for_boundary`` — the same
scoped predicate ``attention-queue`` uses, never the unscoped
``is_latest_committed_greenlight`` (whose own docstring says a consumed ``y``
stays latest forever). Two "what needs me" surfaces that disagree are worse
than one surface, and this is the seam where they would.

The shape is BOUNDED on purpose (``limit`` + ``truncated``): the
relaunch-cheapness invariant says a drain pass that finds nothing drivable
must cost near-zero, and an unbounded projection over ledger HISTORY is
exactly the cost that invariant forbids.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    CampaignId,
    QueueItemId,
    QueueItemState,
    QueueResourceAsk,
    RunIdStrict,
)


class QueueStatusSpec(BaseModel):
    """Input spec for ``queue-status`` — filters and the bound, nothing else."""

    model_config = ConfigDict(extra="forbid", title="queue-status input spec")

    state: QueueItemState | None = Field(
        default=None,
        description=(
            "Restrict to items in this LEDGER state ('queued' or 'placed'). "
            "Null (default) returns both. Note this filters the stored state, "
            "never a projected one — 'show me the parked items' is a filter over "
            "computed facts and would encourage callers to treat a projection as "
            "storage, which is the R1 error."
        ),
    )
    campaign_base: CampaignId | None = Field(
        default=None,
        description="Restrict to items enqueued under this logical campaign base.",
    )
    cluster: str | None = Field(
        default=None,
        description=(
            "Restrict to items pinned to, or already placed on, this clusters.yaml "
            "key. The 'what is queued for carc' read the placement brief needs."
        ),
    )
    include_settled: bool = Field(
        default=False,
        description=(
            "When False (default), items whose projected run is TERMINAL are "
            "omitted from ``items`` (they are still counted). A settled item is "
            "ledger history, and carrying history into every read is the "
            "O(history) startup cost S12 flags against the relaunch-cheapness "
            "invariant. True is the audit read — 'what has this queue ever done'."
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description=(
            "Maximum items returned. The projection is a relayed digest, not a "
            "database cursor; ``truncated`` + ``total_items`` tell the caller "
            "when the bound bit, so a clipped read is visible rather than "
            "quietly partial."
        ),
    )
    now: str | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 UTC 'now' override for deterministic testing (the "
            "doctor / attention-queue precedent). Sets ``computed_at`` and the "
            "instant ages are measured against — never an agent-facing knob for "
            "reshaping ages."
        ),
    )


class QueueStatusItem(BaseModel):
    """One intake item, folded, joined to its projected run facts.

    The two halves are kept visibly separate below: LEDGER FACTS come from the
    fold over the item's intake records; PROJECTED FACTS are recomputed on
    every read and are never written back (R1). If a projected field ever
    starts arriving from intake instead, this docstring is the thing that was
    violated.
    """

    model_config = ConfigDict(extra="forbid", title="queue-status item")

    # ── ledger facts (folded from intake) ─────────────────────────────────────
    item_id: QueueItemId = Field(
        description="The item's identity on the ledger (= its request_id)."
    )
    state: QueueItemState = Field(
        description="Folded ledger state — 'queued' or 'placed', the only two intake stores.",
    )
    enqueued_at: str = Field(
        description="ISO-8601 UTC arrival stamp, pinned to the enqueue record by the fold.",
    )
    updated_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC stamp of the LAST record folded into this item; null if unknown.",
    )
    age_sec: int | None = Field(
        default=None,
        description=(
            "Seconds since ``enqueued_at``, measured against ``computed_at``. "
            "Surfaced because §10.S3 accepts a real cost — a campaign item's "
            "trial params are chosen at ENQUEUE, so a long-held item reflects "
            "the strategy's knowledge then, not now. Age is how a human sees "
            "that the item has gone stale."
        ),
    )
    run_name: str | None = Field(
        default=None, description="Logical run name as enqueued; null when unstated."
    )
    run_id: RunIdStrict | None = Field(
        default=None,
        description=(
            "The item's COMPUTED run id when known (§10.S2). This is the join key "
            "into the run stores AND the occupancy slot key; null means the item "
            "is not yet resolved and holds a slot of its own."
        ),
    )
    campaign_base: CampaignId | None = Field(
        default=None, description="Logical campaign base as enqueued; null for an open-loop item."
    )
    campaign_id: CampaignId | None = Field(
        default=None,
        description=(
            "The concrete ``<base>_<clusterkey>`` campaign id, present only once "
            "the item is PLACED — the cluster is half of the name, so an unplaced "
            "item genuinely does not have one yet."
        ),
    )
    cluster_pin: str | None = Field(
        default=None,
        description="The explicit cluster pin from the enqueue, if any (R5); null otherwise.",
    )
    cluster: str | None = Field(
        default=None,
        description="The cluster actually chosen, present only once the item is PLACED.",
    )
    placement_reason: str | None = Field(
        default=None,
        description=(
            "The disclosed WHY recorded with the placement (R4/§3): every placed "
            "item carries the reason its cluster was chosen, on the durable "
            "record, so the brief that quoted it and the ledger cannot diverge."
        ),
    )
    resources: QueueResourceAsk = Field(
        default_factory=QueueResourceAsk,
        description="The typed resource asks as enqueued — echoed so a hold-back reason is checkable.",
    )
    spec_ref: str | None = Field(
        default=None,
        description=(
            "The spec pointer as enqueued, when the item used the pointer form. "
            "The inline ``spec`` is deliberately NOT echoed here: it can be "
            "arbitrarily large and this result is a bounded digest."
        ),
    )
    retryable: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The DECLARED retryable(n) failure-class budget this item carries "
            "(run-queue plan §7): re-dispatch a MECHANICAL failure terminal up "
            "to n times before parking for a human. Null — the default — means "
            "needs_human. A ledger fact, declared by the enqueuer and only ever "
            "consumed by kernel code (queue-dispatch's declared-retry producer); "
            "a kernel-minted retry item carries the budget it inherited."
        ),
    )
    retries_used: int = Field(
        default=0,
        ge=0,
        description=(
            "How many declared retries this item's chain has spent — the count "
            "of kernel-minted ``.retry<k>`` items on the ledger for its root "
            "(``state/queue_intake.retries_used``, the same durable derivation "
            "the dispatch-side producer charges the budget by). Ledger-derived "
            "on every read, never an in-memory counter, so the number survives "
            "any pass that spent it. 0 for an item with no declared-retry "
            "history; identical across every item of one chain."
        ),
    )

    # ── projected facts (recomputed every read — NEVER stored, R1) ────────────
    dispatched: bool = Field(
        default=False,
        description=(
            "PROJECTION: a RunRecord exists for this item's run_id. This is S10's "
            "resolution in one field — intake never records 'dispatched', because "
            "the submit-once ``submitting`` record already IS the handoff fact and "
            "no write spans both stores atomically."
        ),
    )
    run_status: str | None = Field(
        default=None,
        description=(
            "PROJECTION: the joined RunRecord's journal status (submitting / "
            "in_flight / complete / failed / abandoned), or null when no run "
            "exists yet. Read live from the run store on every call."
        ),
    )
    in_flight: bool = Field(
        default=False,
        description="PROJECTION: the joined run is being monitored (status 'in_flight').",
    )
    terminal: bool = Field(
        default=False,
        description=(
            "PROJECTION: the joined run reached a terminal status. Terminal items "
            "are hidden by default (``include_settled``) but always counted."
        ),
    )
    superseded_by: RunIdStrict | None = Field(
        default=None,
        description=(
            "PROJECTION: the run id that CLOSED this item's run, or null. The same "
            "field ``state/queue_occupancy.run_occupies`` retires a pool slot on "
            "(R9), surfaced because supersession stamps this BEFORE the status "
            "catches up — so for that window a superseded run reads "
            "``terminal=false`` and a drivability test keyed on ``terminal`` alone "
            "would drive a run the occupancy predicate has already retired. Two "
            "surfaces disagreeing about whether a run is finished is exactly what "
            "one shared predicate exists to prevent, so the field is published "
            "rather than left to be inferred."
        ),
    )
    held: bool = Field(
        default=False,
        description=(
            "PROJECTION: the joined run is parked on an ESCALATION verdict "
            "(``RunRecord.pending_verdict`` non-empty), decided by the one "
            "field-as-state predicate ``state/journal.is_held``. This is the "
            "SECOND human-wait axis and it is deliberately not folded into "
            "``parked``: a run may hold on an escalation without ever having "
            "reached a y/nudge boundary, so it carries no pending-decision marker "
            "and no boundary a greenlight could target. A drain loop that tested "
            "``parked`` alone would call such an item drivable and relay ticks at "
            "it forever — the spin this field exists to make decidable."
        ),
    )
    drive_attempts: int = Field(
        default=0,
        ge=0,
        description=(
            "PROJECTION: consecutive agent-facing ``block-drive`` ticks that moved "
            "this run NOTHING (action ``awaiting_decision`` / ``skip``); any tick "
            "that advanced / reran / chained / detached / reached a terminal resets "
            "it to 0. Stamped by ``state/journal.stamp_drive_attempt`` at the one "
            "write point (``ops/block_drive_op``), stored on the RunRecord, and "
            "read back off disk here. It is KERNEL state precisely so a "
            "retryable(n) drain policy survives a relaunch: the budget must not "
            "live in a plan variable that dies with the pass that spent it (§7). "
            "0 for an item with no run yet — nothing has been futilely driven."
        ),
    )
    parked: bool = Field(
        default=False,
        description=(
            "PROJECTION: the joined run carries a pending-decision marker — it is "
            "waiting on a human at a boundary. 'The subagent returns the parked "
            "item to the ledger' is ALREADY TRUE the moment block-drive writes "
            "that marker (§7 R1); there is no return step. Escalation holds "
            "(``pending_verdict``) are a SEPARATE axis, reported as ``held``."
        ),
    )
    park_block: str | None = Field(
        default=None,
        description="PROJECTION: the block slug the run is parked at; null if unparked.",
    )
    awaiting_since: str | None = Field(
        default=None,
        description="PROJECTION: ISO-8601 stamp the park began, straight from the marker.",
    )
    greenlight_committed: bool = Field(
        default=False,
        description=(
            "PROJECTION, and the R7 field: True when a committed greenlight "
            "exists FOR THIS BOUNDARY, decided by "
            "``decision_journal.is_committed_greenlight_for_boundary`` — the same "
            "predicate attention-queue routes through, so the two 'what needs me' "
            "surfaces cannot disagree. Never the unscoped latest-greenlight read, "
            "under which a consumed y stays latest forever."
        ),
    )
    greenlight_unadvanced: bool = Field(
        default=False,
        description=(
            "PROJECTION: parked AND greenlit — the human already said yes and the "
            "work has not moved. attention-queue's highest-leverage class, and the "
            "item a drain pass should claim first."
        ),
    )
    collides_with: list[QueueItemId] = Field(
        default_factory=list,
        description=(
            "Other item_ids on this ledger that resolve to the SAME run_id. §10.S2 "
            "requires this be SAID rather than silently collapsed: two items with "
            "identical resolved params and run_name compute one run id, which for "
            "a hand-enqueued pair is arguably correct (it IS the same experiment) "
            "and for anything else is a duplicate the human must see."
        ),
    )


class QueueStatusResult(BaseModel):
    """The bounded projection — one JSON the session relays (the status-sweep shape)."""

    model_config = ConfigDict(extra="forbid", title="queue-status output data")

    computed_at: str = Field(
        description=(
            "The single instant the whole projection was computed against "
            "(ISO-8601 UTC). One stamp for the read, so a digest looked at an "
            "hour later is visibly an hour-old projection."
        ),
    )
    path: str = Field(
        description=(
            "Absolute path of the intake ledger. Reported even when it does not "
            "exist yet — the read is non-creating (a query must never scaffold "
            "the tree it reports on)."
        ),
    )
    items: list[QueueStatusItem] = Field(
        default_factory=list,
        description=(
            "Matching items in ARRIVAL order (the ledger's own durable order), "
            "clipped to ``limit``. Arrival order, not a priority order: ordering "
            "the human's attention is attention-queue's job and re-deriving it "
            "here would be a second, divergent ranking."
        ),
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Counts over ALL matching items before the limit clip — ledger states "
            "('queued', 'placed') and projected classes ('dispatched', 'in_flight', "
            "'parked', 'held', 'greenlight_unadvanced', 'terminal'). The numbers a "
            "brief quotes, computed over the whole ledger rather than the returned "
            "page."
        ),
    )
    occupancy: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "campaign_id -> occupied pool slots, from the ONE shared predicate "
            "``state/queue_occupancy.occupied_slots`` (R9). Surfaced here so the "
            "number a refill decision rests on is inspectable from the same read "
            "that shows the items, and so a second, subtly different occupancy "
            "count has no reason to be written. Keyed on the campaigns of the "
            "items this call RETURNS — after settled items are hidden and after "
            "``limit`` clips — so it is evidence about the page you are holding, "
            "and so a pass returning zero items does not walk the whole journal "
            "namespace once per campaign (the relaunch-cheapness invariant, §7). "
            "An empty map therefore means 'no returned item names a campaign', "
            "never 'no campaign is occupied'; ask ``queue-advance`` for the "
            "ledger-wide pool arithmetic."
        ),
    )
    total_items: int = Field(
        default=0,
        description="How many items matched the filters before ``limit`` was applied.",
    )
    truncated: bool = Field(
        default=False,
        description="True when ``limit`` clipped ``items`` — a partial read is never silent.",
    )
    skipped_records: int = Field(
        default=0,
        description=(
            "Ledger lines skipped as torn / foreign / unfoldable. A tolerant "
            "reader must never wedge a 'what needs me' path, but it must also not "
            "hide that it dropped something: a non-zero count here is the signal "
            "to go look at the file."
        ),
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "Deterministic, code-computed disclosure lines about the read itself "
            "(a run_id collision, a stale spec_ref, a campaign whose runs were "
            "pruned out from under its items). Never authored prose — each line "
            "restates a fact already in ``items``."
        ),
    )
