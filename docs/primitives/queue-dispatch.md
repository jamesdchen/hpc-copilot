---
name: queue-dispatch
verb: workflow
side_effects:
- file_write: <experiment>/.hpc/queue/intake.jsonl (one placement record per item;
    compacted of settled items after a dispatching tick)
- scheduler-submit: <cluster> (per dispatched item, via campaign-run)
- writes-journal: prunes unreferenced terminal RunRecords after a dispatching tick
    (D10)
idempotent: true
idempotency_key: item.run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent queue-dispatch [--spec <path>] [--experiment-dir <dir>]
  python: hpc_agent.ops.queue.dispatch.queue_dispatch
---
# queue-dispatch

## Purpose

Act on `queue-advance`'s placement decisions — the run queue's **actor**
(`docs/plans/run-queue-placement-2026-07-28.md` §6 Phase 2, resolved by §10).
It is the second half of the authority/actor split copied from
`campaign-advance` / `campaign-refill`: `queue-advance` decides *this item, that
cluster, for this reason* and writes nothing (R3); `queue-dispatch` records the
placement on the intake ledger and starts each item's **normal** run lifecycle.

**It composes; it does not submit (D1).** There is no second submit path here,
no gate of its own, and no consent machinery of its own. A dispatched item is
started exactly the way a `campaign-refill` slot is started today — a detached
`campaign-run` over the item's already-resolved submit-flow spec — so the
greenlight and standing-consent gates bind at the cluster boundary *inside* the
machinery this verb kicks off, where they already bind. A dispatch-side gate
would be a second place the same question is answered, and the two would drift.
Submit **timing** is untouched too: gates first, then submit, exactly as before.
Phase 2 changes *who* triggers a dispatch, not *when* a job enters the scheduler
relative to its gates.

**The claim is the shipped lease, not a new one (D2, §10.S2).** Run ids are
COMPUTED — `"<run_name>-<cmd_sha[:8]>"`, pure and deterministic
(`incorporation/build/compute_run_id`) — so two dispatchers racing one item
derive the *same* id, and the shipped detached single-lease guard
(`_kernel/lifecycle/detached.py::_guard_single_lease`: flock, host and
`create_time`) arbitrates. The loser gets `DetachedLeaseHeld` and this verb
reports it as `claim_held`: a healthy fleet, not a failure. Writing a second
lease system would have produced two claims that can disagree.

**The E4 window is a durable per-campaign lock (D3).** The whole per-item
window runs inside `state/queue_locks.campaign_dispatch_lock`, the durable form
of `campaign-refill`'s sidecar-between-slots rule — because resolving a campaign
trial consumes the optuna scaffold's sidecar index, two overlapping windows
would propose the same trial twice. The lock is re-entrant within a process on
purpose: §10.S3's refill slot holds it across `resolve → enqueue → dispatch`,
and the `queue-dispatch` call nested inside must hold it as well. Its timeout is
caught and reported as `claim_held` too.

**It adopts rather than resubmits, off the local runs store (D4, amended).**
The plan's §10.S2.5 sketch has the scheduler job NAME carry the intake item id.
The shipped backend contract refuses that mechanism outright
(`infra/backends/_engine.py::build_correlation_flags`: SGE caps job names at 15
characters and `job_name` is consumed byte-for-byte by log-path derivation and
canary naming — "the whole reason OPEN-1(iii) was rejected"), and this repo's
own run ids already exceed that cap. **No item id is needed on the scheduler at
all.** Because the run id is computed, the submit-once `submitting` RunRecord is
already the durable "this item is inside its dispatch→id window" fact, and
cluster-side identity rides the correlation token `"<run_id>#<attempt>"`. So the
pre-submit check is one local `load_run` — the same predicate `queue-status`
projects `dispatched` from, so the two surfaces cannot answer one question
differently (R7).

**No new intake state (D8).** Intake stores `{queued, placed}` and nothing
wider; "dispatched" stays a projection over the run stores. The only ledger
write on this path is the placement transition, appended under a token DERIVED
from the item id, so a retried or raced dispatch leaves ONE placement record
rather than one per attempt.

**Nothing is dropped (D9/R4).** Every item this call touched comes back in
exactly one of `dispatched`, `refused` or `held`, each with a reason a human can
act on — successes included, because an adopt that did not say why it adopted is
indistinguishable from a dispatcher that silently did nothing.

### What it deliberately does not do

**It does not resolve.** `resolve-submit-inputs` consumes the optuna scaffold's
sidecar-indexed proposal exactly once (`ops/campaign_refill.py`, rule E4), so a
dispatcher that re-resolved an item its enqueuer had already resolved would
propose the *next* trial and start a run whose id disagrees with the ledger's.
§10.S3 puts the resolve in the refill slot, *before* the enqueue — which is why
`QueueRunSpec` carries `run_id` / `cmd_sha` at all. An item that arrives here
without a startable, already-resolved spec is refused with a stated reason,
never resolved here and never guessed at. That is also why `resolve_blocked` has
no producer in this module (the `courtesy_cap_reached` precedent in
`queue-advance`): the code is reserved for the seat that owns the resolve.

## Inputs

A `QueueDispatchSpec` JSON spec, every field optional:

- `campaign_base` (string, optional) — restrict the call to items enqueued under
  this logical campaign base; forwarded to `queue-advance` unchanged.
- `item_ids` (list of strings, optional) — act on ONLY these items. The refill
  path's field (§10.S3): a slot enqueues one item and dispatches *that* item in
  the same critical section, rather than trusting arrival order to hand it back.
  It NARROWS and never places — an item named here that `queue-advance` HELD is
  reported held, with advance's own reason. An item named here that advance
  returned neither a placement nor a holdback for is looked up on the ledger: an
  already-`placed` item (a dispatch that crashed after the placement append) is
  re-actuated from the placement it already carries, and anything else is
  refused rather than dropped.
- `max_dispatches` (int 1–50, default `1`) — most items to start in this call,
  applied AFTER the authority decided. One by default for the same reason
  `queue-advance.max_placements` is one. A value above 1 must either declare
  the unattended tier (`tier: "unattended"`) or enumerate the batch
  (`item_ids`); the spec refuses a silently raised interactive bound.
- `tier` (`interactive` | `unattended`, default `interactive`) — which tier
  this call runs under. `interactive` keeps the one-decision-per-`y` bound;
  `unattended` is the caller DECLARING the standing-consent tier (the
  retirement wake edge, a scheduled drain), which allows `max_dispatches` > 1.
  A declaration, not a gate pass: consent binds per item at the cluster
  boundary inside the lifecycle this verb composes (D1), so a falsely declared
  tier bypasses nothing — unconsented items come back as disclosed
  `gate_refused` rows or park at their own gate for their own `y`. The basis
  is echoed on the result (`batch_allowed_by`).
- `clusters` (list of strings, optional) — narrow the candidate cluster set;
  forwarded to `queue-advance`. Never overrides a pin (R5).
- `now` (string, optional) — ISO-8601 UTC instant for deterministic testing.
  Sets `computed_at` and is forwarded to `queue-advance`.

`experiment_dir` arrives through the standard `--experiment-dir` CLI arg.

When `item_ids` is given, the internal `queue-advance` call asks for the whole
batch rather than for `max_dispatches`: a named item is not necessarily first in
arrival order, and bounding the authority to one placement would report the
caller's own freshly-enqueued item as `batch_limit_reached`.

## Outputs

A `QueueDispatchResult` with:

- `computed_at` (string) — the instant the call was computed against.
- `stage_reached` (`dispatched` | `nothing_to_dispatch` | `dispatch_refused`) —
  three states, not two: "nothing to do" and "work I could not start" are
  opposite situations for the human.
- `needs_decision` (bool) — true only for a refusal a HUMAN must resolve (a
  refused gate, or a blocked resolve). A held claim is explicitly not one:
  nobody has to decide anything, a peer is already doing the work.
- `dispatched` (list) — one row per item that entered its lifecycle. Each
  carries `item_id`, the claimed `run_id` (required — an item with no derivable
  id is a refusal, never a dispatch), `cmd_sha`, `cluster`, `campaign_id`,
  `outcome` (`started` | `adopted`), `adopted_status` (the existing record's
  status behind an adopt), `placed` (whether THIS call wrote the placement
  record), `detached_pid`, the lifecycle's own `stage_reached`, and the
  disclosed `reason`.
- `refused` (list) — one row per PLACED item that did not start, with a closed
  `reason_code` (`claim_held`, `item_unresolved`, `resolve_blocked`,
  `cluster_unresolvable`, `gate_refused`, `lifecycle_failed`), a specific
  `reason`, the `placed` flag, and a machine-readable `detail`.
- `held` (list) — `queue-advance`'s own holdbacks, relayed VERBATIM: items never
  placed, so the actor had nothing to act on. Passed through rather than
  restated, so the authority's reason is the one the human reads.
- `refused_counts` / `held_counts` (maps) — `reason_code` -> count,
  pre-aggregated so no consumer re-derives a different total.
- `batch_allowed_by` (`unattended_tier` | `item_ids` | null) — why a
  `max_dispatches` > 1 bound was accepted; null for a call at the interactive
  one-per-`y` default. The disclosure for the lifted throttle (R4), never a
  gate outcome.
- `placements_considered` (int) — how many placements `queue-advance` returned
  before any `item_ids` narrowing: the denominator behind the rows above.
- `occupancy` (map) — `campaign_id` -> occupied pool slots as the one shared
  predicate saw them (R9), relayed from `queue-advance`.
- `maintenance` (map) — what the queue's JANITOR did on this tick:
  `dropped_items` / `dropped_records` (settled intake records compacted away),
  `kept_records`, `pruned_runs` (unreferenced terminal RunRecords evicted),
  `protected_runs`, and `error` when a leg failed. Empty `{}` when no grooming
  ran. See the D10 note below.
- `brief` (string) — the deterministic, code-computed disclosure to relay
  verbatim.
- `active_env_overrides` (map) — every `HPC_*` variable exported in this
  process (B15). Dispatch starts cluster-submitting children, so a stray
  transport override reshapes every one of them.

The refusal vocabulary is deliberately disjoint from `queue-advance`'s hold
vocabulary: that one means "I could not CHOOSE a cluster", this one means "a
cluster was chosen and I could not START". Collapsing them would hide which half
of the organ said no.

## Errors

- `spec_invalid` — `now` is not ISO-8601 UTC.

Everything else is DATA. A claim held by a peer, an item with no derivable run
id, a cluster that left `clusters.yaml` between the decision and the dispatch, a
refused gate, a lifecycle that failed on start — all are per-item refusal rows,
because a fleet in which one item's cluster is gone must still dispatch the
other four. An exception the classifier does not recognize is re-raised
untouched: swallowing an unclassified failure would turn a real bug into a note
nobody reads.

## Idempotency

Idempotent through the COMPUTED run id. Two calls that dispatch the same item
derive the same id, so the second one meets three shipped guards in order:

1. the local `load_run` adopt check — an existing `submitting` / `in_flight` /
   `complete` record makes the second call an ADOPT, and nothing is submitted;
2. the detached single-lease guard — a live worker on that id refuses the second
   launch, reported as `claim_held`;
3. the placement append's derived token — a replay writes no second line and
   `placed` comes back false.

A resubmittable terminal (`failed` / `abandoned`) is deliberately NOT adopted: a
corpse is not a dispatch, and the submit-once minter's own decision table mints
a fresh attempt over one.

The placement is appended BEFORE the lifecycle starts (durable-first, the same
ordering §10.S3 uses for enqueue-before-submit). A crash after the start and
before the append would leave a live run whose item still reads `queued`, and the
next `queue-advance` would place it a second time. The cost of the other
ordering is disclosed rather than hidden: a placed-but-unstarted item has left
`queue-advance`'s scope (advance reads only queued items), so `refused.placed`
says so and `queue-status` is the surface that shows it — and naming the item in
a later call's `item_ids` re-actuates it from the placement it already carries.

## Notes

- Reads: the intake ledger, `clusters.yaml` through the normal loader, and the
  runs store (`load_run`). Writes: one placement record per acted-on item.
  Submits: only through the composed `campaign-run`.
- Transport is RE-RESOLVED at use time. A placement's disclosed `ssh_target` is
  stale by design (run12 finding 23), so this verb re-asks `clusters.yaml`
  whether the placed key still exists and still yields a derivable `user@host`,
  and refuses with `cluster_unresolvable` when it does not. It does not rewrite
  the item's submit spec: that spec's target was itself produced by the
  pool-aware `resolve_ssh_target` at resolve time, and overwriting it with the
  config default would silently undo a login-pool failover. The check is a
  refusal gate, not a patch.
- An item whose submit spec names a different cluster than the placement is
  refused too — starting it would submit to a cluster the occupancy arithmetic
  is not counting the item against (R9), so the pool would over-fill silently.
- **D10 — the WRITE authority grooms; the read paths never do.** On a tick that
  actually wrote a placement (and only such a tick), this verb runs
  `ops/queue/maintenance.groom_queue_stores`: compact the intake ledger of items
  whose runs have RETIRED, then prune terminal RunRecords nothing on the ledger
  still references. That restores the run-queue plan's §7 relaunch-cheapness
  invariant, which §8 S12 found violated in two measured places — `queue-status`
  folds the whole append-only ledger, and its `occupancy` leg `load_run`s every
  run file in the namespace. `queue-status` / `queue-advance` declare
  `side_effects: []` and must stay that way: a query that groomed the store it
  reports on would be the F46 bug one layer up. A tick that dispatched NOTHING
  is charged nothing — that pass is exactly what the invariant is about. Items
  this call reports on are exempt from the compaction (never destroy the thing
  you're operating on), and grooming never raises: its trouble is DATA on
  `maintenance.error`, because the items already started.
- `queue-dispatch` is NOT in `SUPPORTED_DETACHED_BLOCK_VERBS` and must not be
  added: it composes `campaign-run`, which detaches itself. Detaching the
  dispatcher would put a second lease between the claim and the work.
- Mechanized by `tests/ops/queue/test_queue_dispatch.py`, which pins each
  binding rule with its negative case — the adopt guard against a resubmittable
  terminal that must NOT be adopted, both held-claim classes (including the one
  that is not an `HpcError`), and the unclassified exception that must escape.
