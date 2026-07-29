"""Pydantic models for the ``queue-run`` mutate verb (run queue, Phase 1).

Wire surface over :mod:`hpc_agent.ops.queue.run`. ``queue-run`` appends ONE
item to the per-experiment intake ledger
(:mod:`hpc_agent.state.queue_intake`) and returns what the ledger now holds.

Two rules from ``docs/plans/run-queue-placement-2026-07-28.md`` shape this
spec more than anything else:

* **R2 — enqueueing is UNGATED.** Putting work on the queue spends nothing:
  no cluster is touched, no budget is drawn, no consent is consumed. Gates
  bind where they always bind, at the cluster boundary of the run the item
  becomes. That is why there is no consent/greenlight field here.
* **R8 / §10.S2 — the client mints a ``request_id``**, consumed as
  ``append_jsonl_line``'s ``dedup_key``, so a relayed workflow turn (the
  engine replays a completed call verbatim from cache) cannot double-enqueue.
  The id is also the item's identity on the ledger.

There is deliberately **no placement field beyond the explicit pin**.
Placement is ``queue-advance``'s authority (R3) and it is disclosed inside the
y (§3); a caller who could assert ``cluster`` + ``campaign_id`` on arrival
would be placing work without the disclosure that makes the human's y mean
something. The one exception is the PIN, which R5 makes supreme over policy —
an operator naming a cluster is a decision, not a guess.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import (
    CampaignId,
    CmdSha,
    QueueItemId,
    QueueItemState,
    QueueResourceAsk,
    RunIdStrict,
)


class QueueRunSpec(BaseModel):
    """Inputs to ``queue-run`` — one item's arrival facts, and nothing else."""

    model_config = ConfigDict(extra="forbid", title="queue-run input spec")

    request_id: QueueItemId = Field(
        description=(
            "Client-minted idempotency token (a UUID4 is the expected shape), "
            "passed straight through as the intake append's dedup_key and "
            "adopted as the item's ``item_id`` (§10.S2). REQUIRED, not derived: "
            "the whole point is that the CLIENT decides which two calls are the "
            "same call, so a workflow engine replaying a cached turn re-sends "
            "the same token and the second append writes nothing. The dedup "
            "probe runs inside the ledger's flock, so it is race-free against a "
            "concurrent duplicate too."
        ),
    )
    spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The spec, recorded VERBATIM on the ledger. Exactly one of "
            "``spec`` / ``spec_ref`` must be given (§1: 'the resolve spec, or a "
            "pointer to it'). Inline is right for a machine-produced item — a "
            "campaign refill enqueuing a trial — because the item then carries "
            "everything a later dispatch needs with no second file to keep "
            "alive. Opaque here: queue-run validates that it EXISTS, never what "
            "is in it; the resolver owns that invariant. WHICH spec depends on "
            "``run_id``: an item carrying a resolved identity (``run_id`` + "
            "``cmd_sha``) must record the RESOLVED submit-flow spec "
            "(``ResolveSubmitInputsResult.submit_spec``), because queue-dispatch "
            "composes the shipped lifecycle and never re-resolves — a second "
            "resolve would write another sidecar, consume the NEXT optuna "
            "proposal, and derive a run_id disagreeing with the one on the "
            "ledger (run-queue plan §10.S3/E4/D5). An item with no resolved "
            "identity may record an unresolved resolve spec; dispatch refuses it "
            "as ``item_unresolved`` rather than resolving on its behalf."
        ),
    )
    spec_ref: str | None = Field(
        default=None,
        description=(
            "Pointer to a resolve spec on disk, for an item whose spec is large "
            "or already versioned in the repo. Exactly one of ``spec`` / "
            "``spec_ref``. Recorded verbatim and NOT dereferenced at enqueue: a "
            "pointer that goes stale is a fact the projection should surface, "
            "not a reason to refuse arrival."
        ),
    )
    run_name: str | None = Field(
        default=None,
        description=(
            "Optional logical run name. Load-bearing for §10.S2: run ids are "
            "COMPUTED, not minted — ``run_id = '<run_name>-<cmd_sha[:8]>'`` — so "
            "supplying it is what lets the item carry a run_id while it is still "
            "on the ledger, which is in turn what lets the occupancy predicate "
            "collapse the item and the run it becomes onto ONE slot. Omit it and "
            "the item simply holds its own slot until it resolves."
        ),
    )
    run_id: RunIdStrict | None = Field(
        default=None,
        description=(
            "The item's COMPUTED run id, supplied by a caller that ALREADY "
            "resolved it (§10.S3: campaign-refill resolves first, then enqueues, "
            "because resolving consumes the optuna sidecar index exactly once). "
            "Never REQUIRED and never derived here — an unresolved hand enqueue "
            "stays legal and simply carries no identity yet. Recording it is a "
            "correctness precondition rather than a convenience: the occupancy "
            "predicate collapses an item and the run it becomes onto ONE slot "
            "only when the item knows its run_id, so without it the "
            "enqueue-then-dispatch handoff counts one committed slot twice. It "
            "asserts nothing about a run EXISTING — the id is a pure function of "
            "run_name and cmd_sha; whether a run is live is the run stores' fact "
            "(R1)."
        ),
    )
    cmd_sha: CmdSha | None = Field(
        default=None,
        description=(
            "The resolved parameter identity behind ``run_id`` — its pre-image, "
            "since ``run_id`` keeps only the first 8 hex characters. Supplied "
            "with ``run_id`` by a caller that resolved first; null otherwise. "
            "Carried so a later dispatcher that finds an existing run for this id "
            "can state WHICH cmd_sha the ledger committed to instead of inferring "
            "agreement from a truncation."
        ),
    )
    cluster: str | None = Field(
        default=None,
        description=(
            "Optional EXPLICIT cluster pin — a clusters.yaml top-level key. R5: "
            "a pin always wins over policy, because an operator naming a cluster "
            "is a decision and the placement default is only a default. This is "
            "the sole placement input a caller may assert; the chosen cluster of "
            "an unpinned item is ``queue-advance``'s to decide and to disclose. "
            "A pin naming a key absent from the active clusters.yaml is REFUSED "
            "here (cluster_unknown, with the near-miss suggestion; a BLANK pin is "
            "spec_invalid, since omitting the field and blanking it mean opposite "
            "things): R5 makes the pin "
            "supreme, so nothing downstream would ever re-choose, and enqueueing a "
            "typo would convert it into a permanently unplaceable item whose "
            "hold-back reason surfaces hours later. queue-advance's "
            "``cluster_pin_unknown`` hold covers the other case — a pin that was "
            "valid at enqueue and went stale under a later clusters.yaml edit."
        ),
    )
    campaign_base: CampaignId | None = Field(
        default=None,
        description=(
            "Optional logical campaign base. Placement composes the concrete "
            "campaign id as ``<base>_<clusterkey>`` (docs/design/"
            "campaign-multi-cluster.md §2), which is how one study splits across "
            "CARC and Hoffman2 while the shared-study merge still reunifies the "
            "results. Null for an open-loop item, which belongs to no campaign "
            "and occupies no campaign pool slot."
        ),
    )
    resources: QueueResourceAsk = Field(
        default_factory=QueueResourceAsk,
        description=(
            "Typed resource asks (§9's TYPED ruling) used as the hard-constraint "
            "leg of placement. Defaults to an empty ask, which means every "
            "configured cluster satisfies the item."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_spec_source(self) -> QueueRunSpec:
        """Refuse an item with no spec, or with two.

        A ledger row that names no work is unactionable, and one naming work
        two ways invites the two sources to disagree after the fact — the item
        would then be dispatched from whichever the reader happened to prefer.
        A primitive owns its invariants: refuse at the boundary rather than
        write an ambiguous record that a later phase has to guess about.
        """
        has_spec = self.spec is not None
        has_ref = bool(self.spec_ref and self.spec_ref.strip())
        if has_spec and has_ref:
            raise ValueError("give exactly one of spec / spec_ref, not both")
        if not has_spec and not has_ref:
            raise ValueError("one of spec / spec_ref is required")
        if has_spec and not self.spec:
            raise ValueError("spec must be a non-empty object")
        return self

    @model_validator(mode="after")
    def _resolved_identity_is_whole(self) -> QueueRunSpec:
        """Refuse half a resolved identity.

        ``run_id`` is ``"<run_name>-<cmd_sha[:8]>"`` — three facts that are one
        fact. A record carrying the id without its pre-image cannot say which
        parameters it committed to, and one carrying a cmd_sha with no id
        commits to nothing an occupancy scan or a dispatcher can key on. Both
        would be enqueued happily and only fail hours later, at the point where
        someone tries to join on the missing half; a primitive owns its
        invariants, so the seam refuses instead.

        Absent entirely stays legal and is the common case (a hand enqueue is
        unresolved by construction, and R2 says arrival spends nothing —
        including the cost of resolving).
        """
        if (self.run_id is None) != (self.cmd_sha is None):
            raise ValueError(
                "run_id and cmd_sha are one fact (run_id = '<run_name>-<cmd_sha[:8]>'): "
                "give both or neither"
            )
        if self.run_id is not None and not (self.run_name and self.run_name.strip()):
            raise ValueError(
                "run_id was supplied without run_name — the id is DERIVED from the "
                "run name, so a record carrying one without the other cannot be "
                "recomputed or checked"
            )
        return self


class QueueRunResult(BaseModel):
    """What the ledger holds after the enqueue — including on a replay."""

    model_config = ConfigDict(extra="forbid", title="queue-run output data")

    path: str = Field(
        description="Absolute path of the intake ledger the item lives on.",
    )
    item_id: QueueItemId = Field(
        description=(
            "The item's identity on the ledger — equal to the request_id "
            "(§10.S2). Every later transition record keys on this, and the "
            "current state of the item is the fold over its records."
        ),
    )
    request_id: QueueItemId = Field(
        description="Echo of the client's idempotency token, so a relay can confirm the match.",
    )
    state: QueueItemState = Field(
        description=(
            "The item's state as written: always 'queued' on arrival. An item "
            "cannot ARRIVE placed — placement is a separate, disclosed decision."
        ),
    )
    replayed: bool = Field(
        default=False,
        description=(
            "True when this request_id was already honoured: NO line was "
            "written. The honest signal a relay needs — 'you already asked for "
            "this' is a different fact from 'I enqueued it', even though both "
            "are success. ``record`` is the ORIGINAL item, unless ``compacted`` "
            "is also true (see there)."
        ),
    )
    compacted: bool = Field(
        default=False,
        description=(
            "True when this request_id replayed against a COMPACTION TOMBSTONE "
            "rather than against a live ledger row: the item was enqueued, "
            "dispatched, its run settled, and the janitor removed its records "
            "(``state/queue_intake.compaction_tombstones``). ``replayed`` is true "
            "with it and nothing was written — which is the whole point, because "
            "without the tombstone the dedup probe would MISS a compacted item "
            "and re-enqueue a run that already finished (R8). ``record`` then "
            "carries the tombstone's stated reason instead of the original "
            "arrival facts, which are gone; ``run_id`` / ``cmd_sha`` are null and "
            "``enqueued_at`` is empty for the same reason. Never true together "
            "with a ``record`` the fold produced."
        ),
    )
    run_id: RunIdStrict | None = Field(
        default=None,
        description=(
            "The item's COMPUTED run id as the LEDGER holds it (§10.S2: "
            "'<run_name>-<cmd_sha[:8]>', a pure query, never minted); null for an "
            "item that arrived unresolved and has learned nothing since. Read "
            "back from the fold, so a replay reports the ORIGINAL item's id — "
            "including one a later placement record supplied. Two items with "
            "identical resolved params and the same run_name compute the SAME "
            "run_id; that collision is real and ``queue-status`` names it rather "
            "than silently collapsing the rows."
        ),
    )
    cmd_sha: CmdSha | None = Field(
        default=None,
        description=(
            "The item's resolved parameter identity as the LEDGER holds it; null "
            "for an unresolved item. Read back rather than echoed from the spec, "
            "which is the whole point on a replay: it reports what the original "
            "item committed to, so a caller re-sending a request_id with a "
            "different resolution sees the mismatch instead of assuming its own."
        ),
    )
    enqueued_at: str = Field(
        description=(
            "ISO-8601 UTC arrival stamp of the item (of the ORIGINAL item on a "
            "replay). §10.S3's accepted cost — trial params are chosen at "
            "enqueue — is why enqueue age is a fact worth surfacing."
        ),
    )
    queued_count: int = Field(
        default=0,
        description=(
            "How many items on this ledger are currently in state 'queued' "
            "(after the append). The one number a relay can act on without a "
            "second call: it is the depth the next queue-advance will consider."
        ),
    )
    record: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The intake record as written (or as found, on a replay) — echoed "
            "so the session can relay exactly what the ledger now holds rather "
            "than a restatement of it."
        ),
    )
