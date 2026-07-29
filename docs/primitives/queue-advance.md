---
name: queue-advance
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent queue-advance [--spec <path>] [--experiment-dir <dir>]
  python: hpc_agent.ops.queue.advance.queue_advance
---
# queue-advance

## Purpose

Decide WHERE the queued items go, and disclose why — the run queue's placement
**authority** (`docs/plans/run-queue-placement-2026-07-28.md` §2, §3, §6). It
copies the proven `campaign-advance` / `campaign-refill` split: this verb reads
the intake ledger, `clusters.yaml`, and the one shared occupancy predicate, and
returns a DECISION — *this item, that cluster, under that `<base>_<clusterkey>`
campaign id, for this stated reason*. `queue-dispatch` (Phase 2) is the only
thing that will ever act on it.

**It is pure (R3).** No file is written, no watermark moved, no cache minted,
no cluster contacted; the ledger read is routed through the non-creating path
accessor so a decision against a virgin experiment does not scaffold the tree
it is reporting on. Purity is not hygiene here. §3 puts the placement INSIDE
the human's `y` — the greenlight binds to a spec and the spec names the cluster
— so a decision that could not be recomputed byte-for-byte from the same inputs
would make the `y` unfalsifiable.

**The policy is a shipped deterministic default, not a heuristic** (§3), in
four steps:

1. **An explicit cluster pin always wins (R5).** A pinned item is evaluated
   against its pin and nothing else. If the pin fails a hard constraint the
   item is HELD with that reason — never quietly re-placed, because re-placing
   it would be the tool overruling an operator on a decision the operator
   already made. The `clusters` restriction narrows *policy's* candidates; it
   never narrows an operator's.
2. **Filter by hard constraints** derived from the item's typed resource asks
   against the cluster's DECLARED values: `gpu` against
   `gpu_types`/`gpu_queues`/`gpu_constraint`, `gpu_type` against `gpu_types`
   (documented informational — promoting it to a hard filter is disclosed in
   the verdict), `walltime_sec` against the top-level `max_walltime_sec`, and
   `est_core_hours` against `constraints.max_estimated_core_hours` (the
   cluster's own declared cost gate, which would refuse the submission). Checks
   run in a fixed order and the first failure wins, so the reason a human reads
   is deterministic. A `cores` ask is reported as *not compared*: `clusters.yaml`
   declares no per-cluster core ceiling and none is invented.
3. **Least-loaded** by `state/queue_occupancy.occupied_slots` — the ONE shared
   "occupies a pool slot" predicate (R9), which both this verb and (later)
   `campaign-advance` consume so the two cannot place against different totals.
4. **Stable alphabetical tie-break**, so equal load resolves the same way every
   time.

**It sheds the capacity question entirely (R6, §7 R3).** Slurm/SGE already run
the authoritative resource queue — fairshare, priorities, backfill,
walltime-aware placement — and their state is the one thing we cannot predict
from outside. So this verb never probes a scheduler, never models queue depth,
and never infers headroom; there is no `no_capacity` hold reason. What remains
ours is cross-cluster choice (each scheduler sees only its own queue), courtesy
caps, and hard-constraint mismatch. Phase 1 ships **no courtesy cap**: the only
per-cluster cap in the config is `constraints.max_concurrent_jobs`, and S11
confirmed it is per-submission-plan wave grouping (`compute_submission_plan`
reads only `(constraints, workload)` — no journal, no other runs), so citing it
as a cross-run account cap would invent a second meaning for a field that
already has one. A real account-level pending cap needs its own config key.

**Nothing is dropped and nothing is guessed (R4).** Every queued item in scope
comes back either as a placement or as a holdback with a closed `reason_code`
and a specific human-readable reason, carrying the per-cluster verdicts behind
it so the choice can be checked rather than trusted.

## Inputs

A `QueueAdvanceSpec` JSON spec with:

- `campaign_base` (string, optional) — restrict the decision to items enqueued
  under this logical campaign base. Null (the default) considers every queued
  item. Out-of-scope items are not held; they were never this call's business.
- `max_placements` (int 1–50, default `1`) — most placements to decide in this
  call. One by default because placement is disclosed inside a `y` and a human
  signs one decision at a time. Items past the bound are still EVALUATED and
  reported as `batch_limit_reached` with the cluster they would have taken, so
  a caller-imposed bound is never confusable with a constraint failure.
- `clusters` (list of strings, optional) — narrow the candidate set to these
  `clusters.yaml` keys. It can never place an item on a cluster the hard
  constraints reject, and it never overrides a pin (R5).
- `now` (string, optional) — ISO-8601 UTC evaluation instant for deterministic
  testing (the `doctor` / `attention-queue` precedent). Sets `computed_at`;
  never a knob for reshaping the decision.

`experiment_dir` arrives through the standard `--experiment-dir` CLI arg.

## Outputs

A `QueueAdvanceResult` with:

- `computed_at` (string) — the single instant the decision was computed against.
- `decision` (`place` | `hold` | `empty`) — three states, not two: "nothing to
  do" and "something to do that I refused to guess at" are opposite situations
  for the human.
- `placements` (list) — at most `max_placements`, in dispatch order. Each
  carries `item_id`, `cluster`, the composed `campaign_id`, `scheduler` and
  `ssh_target` (disclosure only — a run's target is re-resolved at use), the
  `pinned` flag (an operator's own choice is never presented back as the tool's
  recommendation), the `reason` the `y` is taken against, and `considered` —
  every cluster evaluated with its verdict.
- `held` (list) — every item that stays queued, each with `reason_code`
  (`no_clusters_configured`, `cluster_pin_unknown`,
  `no_cluster_matches_constraints`, `courtesy_cap_reached`, `item_unresolved`,
  `batch_limit_reached`), a specific `reason`, the `considered` verdicts, and a
  machine-readable `detail` (the unknown pin and its near-miss suggestions, the
  asks that found no host, the bound and the cluster it displaced).
- `held_counts` (map) — `reason_code` -> count, pre-aggregated so a morning
  brief renders "3 items waiting on gpu" without re-deriving a different total.
- `queued_total` (int) — in-scope items in state `queued` at decision time.
- `considered_clusters` (list) — the candidate keys evaluated, after any
  restriction. An unexpectedly narrow set is the likeliest cause of a
  surprising hold, so it is published.
- `occupancy` (map) — `campaign_id` -> occupied pool slots, straight from
  `occupied_slots` (R9), so the decision's arithmetic is checkable.
- `brief` (string) — the deterministic, code-computed disclosure the session
  relays verbatim into the `y`.

## Errors

- `spec_invalid` — `now` is not ISO-8601 UTC.

Everything else that could go wrong is DATA, not an error. An unreadable or
empty `clusters.yaml` holds every item with `no_clusters_configured`; one
broken cluster stanza is a single ineligible verdict, not a wedged decision; an
item whose `resources` blob does not parse, or which names neither a `spec` nor
a `spec_ref`, is held as `item_unresolved` rather than placed as though it had
asked for nothing. R4 makes a held item a normal outcome of a healthy queue.

## Idempotency

Idempotent and side-effect-free: a query that writes nothing is trivially
replay-safe, and the empty `side_effects` list is that promise mechanized.
Two calls over the same ledger and the same `clusters.yaml` return
byte-identical results — the placement policy is a total order over local data
at every step.

Within ONE call, a placement decided earlier provisionally occupies its
cluster's slot for the rest of the pass, so a batch of three does not stack on
one cluster merely because none of them has been dispatched yet. That
provisional increment is disclosed in the reason and deliberately kept out of
the per-cluster `occupied` field, which stays exactly the shared predicate's
number.

## Notes

- Reads: `<experiment>/.hpc/queue/intake.jsonl` (via
  `state/queue_intake.read_intake_items`, non-creating), `clusters.yaml`
  through the normal loader resolution order, and `state/queue_occupancy` for
  load. No SSH, no scheduler, no journal write.
- Campaign ids are COMPOSED (`<base>_<clusterkey>`, `campaign-multi-cluster.md`
  §2), never parsed — a base routinely contains underscores, so splitting one
  back apart is a hazard. A cluster key that cannot form a legal campaign id
  for the item's base is reported ineligible rather than coerced.
- `clusters.yaml` carries TWO walltime ceilings that disagree by 8–16×: the
  top-level `max_walltime_sec` ("cluster's hard walltime ceiling") and
  `constraints.max_walltime` (the throughput planner's per-array ceiling,
  defaulted to `24:00:00` even where no constraints block exists). Placement
  compares the hard one — the question is whether the scheduler will accept the
  job — and every walltime verdict names which it compared, quoting the other.
- Mechanized by `tests/ops/queue/test_queue_advance.py`, which pins each
  binding rule with its negative case, including a source-level check that the
  module routes occupancy through the shared predicate instead of re-inlining a
  run scan.
