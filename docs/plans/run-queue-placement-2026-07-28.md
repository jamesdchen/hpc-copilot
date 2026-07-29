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

## 7. v2 — the ledger loop (2026-07-29, maintainer's synthesis)

The maintainer's restatement: "a ledger... a dynamic workflow that spawns
agents as this ledger fills and drains... subagents take jobs off one by
one... return parked stuff to the ledger, which reports to the main session
for human decision... nudge loops in main until y, then back on the ledger
for another subagent... optuna trials automatically unpark." Two
refinements make this buildable without new trust surface:

**R1 — the ledger is an INDEX, not a second journal.** Only INTAKE is a
new store (§1). "Parked", "drivable", "in-flight", "terminal" are
PROJECTIONS over state that already exists and is already durable:
pending-decision markers + resume cursors (`state/journal.py`), committed
greenlights (decision journal), detached-watch leases, RunRecords. A
ledger that COPIED park state would drift from the journal the first time
a y landed; a ledger that projects it cannot. Concretely: "the subagent
returns the parked item to the ledger" is ALREADY TRUE the moment
block-drive writes the pending marker — no return step exists to build.
"After the y it's placed back on the ledger" is ALREADY TRUE the moment
the y commits — the drivable-runs projection includes it instantly. The
new verb `queue-status` = intake items ⋈ those projections, one bounded
JSON (the `status-sweep` composite pattern).

**R2 — subagents RELAY, the kernel DRIVES, the ledger DECIDES nothing.**
"Subagents drive jobs to completion" must not mean judgment: each worker
loop is plan code relaying `block-drive` ticks for its claimed item (the
campaign-run loop, N of them). Intelligence placement is unchanged:
sequencing in the kernel, decisions at the parks with the human,
admission/placement in `queue-advance`.

**The loop, end to end:**

1. **Fill** — main session (or campaign refill, see 6) `queue-run`s items.
2. **Spawn to depth** — the drain workflow launches; loop count =
   min(drivable items, maxLoops, `queue-advance` capacity). Each loop
   CLAIMS one item (a claim LEASE — the detached-lease pattern keyed
   `(run_id, driver_id)`, so two workflows/sessions never double-drive;
   ticking is race-safe regardless, claims are hygiene not safety).
3. **Drive** — tick relays; detached waits held inside the loop (chunked
   `wait-detached timeout_sec` + heartbeat log).
4. **Park** — a gate/failure ends that loop; the park is durable state
   (R1). The loop's claim releases. The workflow keeps driving OTHER items.
5. **Report** — the workflow returns when NOTHING is drivable: merged park
   queue + `queue-status` snapshot. This return IS "the ledger reports to
   the main session" (plus `attention-queue` remains readable any time).
6. **Decide** — nudge loops in main, code never reading a nudge string;
   the y commits against the resolved spec. Auto-resume relaunches the
   drain workflow; the freshly-drivable item is claimed by a new loop —
   the maintainer's "another subagent picks it up", for free.
7. **Auto-unpark (campaigns)** — two existing mechanisms, no new one:
   a greenlit async campaign's refill actor ENQUEUES next trials
   (campaign-refill becomes a ledger producer), and standing/overnight
   consent lets consented boundaries auto-advance so those items never
   park at all. "Optuna unparks the ledger" = producer + consent, both
   shipped.

**R3 — the scheduler IS the capacity queue (2026-07-29).** Maintainer:
"take advantage of the fact that many hpc systems already have a queuing
system." Slurm/SGE already run the authoritative resource queue —
fairshare, priorities, backfill, walltime-aware placement — and their
state is the one thing we cannot predict from outside. So our ledger must
NOT gate on inferred capacity. Two queues in series, each doing what only
it can:

| | our ledger | the scheduler's queue |
|---|---|---|
| holds work until | human evidence (y / consent) + placement chosen | resources free |
| authority | gates, budget/consent caps, cross-cluster choice | fairshare, backfill, node allocation |
| asynchrony | parks + relaunch | pending → running, watched by the detached child |

Consequences:

- **Submit eagerly.** Once an item is greenlit/consented and placed, it
  goes INTO the scheduler queue and sits there as `pending` — the detached
  watch already covers that lifecycle. The ledger's "queued" state is
  therefore almost entirely PRE-GATE (awaiting y, consent, or placement),
  not awaiting capacity.
- **`queue-advance` sheds the capacity question.** What remains OURS is
  only what the scheduler cannot know: (a) cross-cluster choice — each
  scheduler sees only its own queue; placement inputs are our own
  in-queue count per cluster and a cheap pending-depth probe, advisory
  and disclosed; (b) courtesy caps — `clusters.yaml` fields like
  `max_concurrent_jobs` are lab-etiquette/anti-throttle POLICY (the
  connection-storm lineage), enforced by us because centers penalize
  queue-flooding accounts; (c) consent budget caps, which bind spend, not
  slots.
- **`no_capacity` nearly vanishes** as a ledger state: an item held back
  is held for etiquette caps or a hard constraint mismatch, and the
  disclosed reason says which — never a guess about scheduler headroom.

**Latency + residency invariants (2026-07-29, adopted from the latency
review):**

- **Relaunch-cheapness invariant.** A drain pass that starts, finds
  nothing drivable, and returns must cost near-zero: one `queue-status`
  relay, no loops spawned. This is load-bearing for the whole
  chain-of-relaunches model — "just relaunch it, whenever, for any
  reason" must be the correct reflex, never something to economize. Any
  future pass-startup work that scales with ledger HISTORY (rather than
  with currently-drivable items) violates this.
- **Held waits count as drivable (default return policy).** A pass
  returns only when every claimed item is parked-on-human or terminal;
  items sitting in detached waits keep the pass alive (chunked
  `wait-detached timeout_sec` + heartbeat). `wait-detached` returns AT
  the lease terminal, so event→action latency is ~0 while a pass lives —
  the only dead-air case left is no-live-session, which is the wake
  tier's job, not the workflow's.
- **First tick after the y goes inline.** The post-greenlight moment is
  the one latency-sensitive interactive step (~15–30s if routed through a
  relaunch). The main session ticks `block-drive` directly, inline, the
  moment the y commits — instant visible motion, one tick of transcript
  cost — and the auto-resumed drain pass takes over from the second tick.
- **The unattended tier is watch → poke → drain.** Capture is the
  kernel's (the armed detached `status-watch` survives session death and
  records terminals durably the moment they happen); wake is the
  harness's (a scheduled re-poke where supported — CCR `send_later` /
  routines — else the human's next sit-down); catch-up is the drain
  pass's (stateless, reads what the watch recorded, advances what consent
  covers, parks the rest). No layer substitutes for another, and only
  the wake leg is harness-dependent.

- **`wait-any-detached` (Phase 2 efficiency).** Holding N waits as N
  chunked relay loops costs held-hours × runs in small agent calls. A
  kernel verb blocking until ANY of a lease set resolves (select over
  lease pids, same `timeout_sec` chunk contract) collapses the pass to
  ONE holder for all in-flight runs — cost per night becomes flat, and
  the plan wakes knowing which run resolved. Rationale, stated once: a
  held wait is the only EVENT-grade bridge into a turn-based session
  (kernel children cannot inject turns; pokes are time-based), so
  hold-while-alive is the proper design and this makes it cheap.

**Design decisions still open (v2 additions):**

- Claim lease TTL/steal policy: a driver that dies mid-claim — lease
  expiry by heartbeat staleness (reuse `pid_alive` probe) vs. explicit
  `queue-release`. Proposed: heartbeat staleness, the detached precedent.
- Failure classes on parked items: `needs_human` vs `retryable(n)` as
  DECLARED item data consumed by plan code — never an agent's judgment
  call. Proposed: default needs_human; retryable only by explicit intake
  flag.

## 8. Open questions for the maintainer

- Resource-ask vocabulary on intake items: free-form (opaque, policy reads
  known keys) vs typed (schema'd gpu/cores/walltime)? Typed proposed.
- Should an item be allowed to SPLIT (its task grid sharded across two
  clusters)? Proposed NO for v1 — an item is atomic; shard by enqueuing two
  items. Sharding one array across schedulers multiplies every failure mode.
- Cross-EXPERIMENT queue (one queue over many repos) — proposed NO: the
  queue is per-experiment-repo like every other store; a lab-level view is
  a reporting sweep, not a store.
