# The run queue + cluster placement — design proposal (2026-07-28)

Status: **BANKED — design discussion with the maintainer, nothing built.**
Motivating order: "there needs to be a queue that keeps track of multiple
experiments to run as they come in and it needs to assign the runs to the
proper clusters / split across clusters so that there's asynchroneity that
allows things to be seamless."

## 0. What already exists (build on, don't duplicate)

Three of the four organs are shipped:

| Organ | Shipped as | Where |
|---|---|---|
| Cluster isolation | one repo, N campaign_ids, `<base>_<clusterkey>` naming; a cid is one cluster BY CONSTRUCTION | `docs/design/campaign-multi-cluster.md`, `state/index.py::find_runs_by_campaign` |
| Async submission | authority/actor split: `campaign-advance` decides "refill n", `campaign-refill` submits detached | `ops/campaign_refill.py`, `meta/campaign/atoms/advance.py` |
| Unattended boundaries | greenlight as standing consent for refill; overnight consent with hard caps + armed wake | `ops/overnight.py`, block gates |
| **Intake + placement** | **MISSING** — arrival of new work and the choice of WHICH cluster are both manual today | this proposal |

The missing organ is exactly two decisions: *what runs next* (a queue) and
*where it runs* (placement). Everything downstream of those decisions —
isolation, detached driving, refill, parks, consent — already works.

## 1. The queue is kernel state, never workflow memory

Experiments "come in" from any session at any time, and workflows are
stateless relays that die and replay from cache — so the queue must be a
durable kernel store, not something a dynamic workflow holds:

- `.hpc/queue/intake.jsonl` — append-only arrival ledger (the
  `append_jsonl_line` discipline), one record per requested run: the resolve
  spec (or a pointer to it), resource asks (gpu? est core-hours? walltime),
  optional cluster pin, optional campaign base.
- Item state rides the existing runs/journal stores once dispatched; the
  queue itself only tracks `queued → placed → dispatched` (dispatch hands
  off to the normal run lifecycle — the queue never mirrors run status).
- New verb `queue-run` (mutate, **ungated**): enqueueing spends nothing.
  Gates bind where they always bind — at the cluster boundary of the run
  the item becomes.

## 2. Authority/actor, again

Copy the proven `campaign-advance`/`campaign-refill` split:

- **`queue-advance` (pure authority, query):** reads the intake ledger +
  per-cluster load (in-flight counts per cid — the existing partition — plus
  `clusters.yaml` caps like `max_concurrent_jobs`, plus at most one
  throttled `batch-status` probe per cluster) and returns a DECISION:
  `{item, cluster, campaign_id: "<base>_<clusterkey>", reason}` or
  `no_capacity` / `empty`. Deterministic, no I/O writes, unit-testable.
- **`queue-dispatch` (actor, workflow verb):** consumes the decision:
  resolve → compose the submit brief → the normal gate. Detached
  (`campaign_run(detach=True)` precedent), sequential per the RFC E4
  sidecar-between-slots rule when multiple items dispatch to one cid.

## 3. Placement lives INSIDE the y

The load-bearing rule. A greenlight binds to a spec; the spec must name the
cluster. So placement is resolved BEFORE the brief, and the brief discloses
it: *"next: rv_sweep item 4 → hoffman2 (carc at 2/2 concurrent jobs, item
needs gpu, hoffman2 queue depth 3) — y?"*. The human's y covers the
placement. Under standing/overnight consent the same policy runs
unattended, and the consent's caps bind per cluster (a consent names its
scope — extending consent vocabulary to `{cluster: cap}` maps is Phase 2).

Policy posture, per the tool doctrine:

- a SHIPPED deterministic default (filter by hard constraints from the
  item's resource asks vs cluster caps; then least-loaded; then stable
  alphabetical tie-break) whose chosen reason is always disclosed;
- an item's explicit `cluster:` pin always wins;
- no qualifying cluster → the item STAYS QUEUED with a disclosed reason
  (never dropped, never guessed); `queue-advance` reports it so the brief /
  morning brief can surface "3 items waiting on gpu capacity".

## 4. "Split across clusters" = placement over existing cids

No new isolation primitive. Splitting one study across CARC + Hoffman2 is
the placement policy choosing `rv_sweep_carc` vs `rv_sweep_hoffman2` per
item — the shared-study merge (`campaign-multi-cluster.md` §4–5) already
reunifies results for reporting. The queue makes the split *dynamic* (next
item goes wherever headroom is) instead of manual (human picks the cid).

## 5. Asynchronicity: the tick, the wake, the workflow

- `queue-drive` is a stateless tick (block-drive's shape): one invocation
  admits at most what capacity allows, then exits. Durable state only.
- Wakes: the detached watch terminals that already exist are the capacity-
  freed signal — a run finishing IS the moment to re-tick the queue. The
  overnight wake-leg machinery (`status_watch_armed`) is reused unchanged.
- The dynamic workflow (`campaign-run` generalized, or a sibling
  `queue-drain` plan) relays `queue-drive` ticks exactly as it relays
  `block-drive` ticks today — rule-5 authored templates, N run-loops for
  the in-flight runs, parks merged, return when all loops are quiescent.
  The workflow remains a relay: the queue, the placement, and every
  admission decision are kernel code it cannot influence.

## 6. Phasing

1. **Phase 1 — the store + authority:** `queue-run`, intake ledger,
   `queue-advance` with the default policy, `queue-status` projection.
   Placement disclosed in briefs; dispatch still human-triggered.
2. **Phase 2 — the actor + consent vocabulary:** `queue-dispatch` detached;
   standing-consent scopes learn per-cluster caps; morning brief gains the
   queued-items section.
3. **Phase 3 — the workflow relay:** N run-loops + queue ticks in one plan;
   merged park queue; park-with-context enrichment.

## 7. Open questions for the maintainer

- Resource-ask vocabulary on intake items: free-form (opaque, policy reads
  known keys) vs typed (schema'd gpu/cores/walltime)? Typed proposed.
- Should an item be allowed to SPLIT (its task grid sharded across two
  clusters)? Proposed NO for v1 — an item is atomic; shard by enqueuing two
  items. Sharding one array across schedulers multiplies every failure mode.
- Cross-EXPERIMENT queue (one queue over many repos) — proposed NO: the
  queue is per-experiment-repo like every other store; a lab-level view is
  a reporting sweep, not a store.
