# The run queue + cluster placement — design proposal (2026-07-28)

Status: **PHASES 1+2 SHIPPED (2026-07-29) — §6's store, authority, and actor
are live; the S1 consent leg is wired through both scopes.** Remaining: the
Phase-3 drain loop (maintainer-ordered), content-addressed trees + eager
submit (probe-cleared, §10.S4), per-cluster consent caps, brief-UX bundle.
Design complete through v2 + adversarial sweep. Motivating order: "there
needs to be a queue that keeps track of multiple experiments to run as they
come in and it needs to assign the runs to the proper clusters / split
across clusters so that there's asynchroneity that allows things to be
seamless."

## PICKUP (for a fresh session, 2026-07-29)

Read this file top to bottom — it is self-contained. The state of play:

1. **The architecture is settled** (§1–§7): ledger-as-index over existing
   durable state (only INTAKE is a new store), authority/actor split
   (`queue-advance`/`queue-dispatch`), placement inside the y, scheduler
   as the capacity queue (submit eagerly), a drain workflow of N
   campaign-run-shaped loops whose manager is the plan SCRIPT (state in
   variables + journals, never model context — the tokenomics depend on
   this), hold-while-alive / poke-when-dead / capture-always residency.
2. **The fable-sweep verdict (§8) is the pre-build checklist.** Thirteen
   confirmed clusters. S5/S6 are FIXED in the shipped campaign-run.js.
   **S1–S4 are RESOLVED in §10** — the blocking gate is cleared. S7–S13
   land incrementally with their named resolutions.
3. **Phases 1+2 are SHIPPED (2026-07-29)**: intake store, `queue-run` /
   `queue-status` / `queue-advance` / `queue-dispatch`, refill as ledger
   producer, campaign `placement_scope`, the morning digest's `queue`
   section. Adversarially reviewed before landing (13 findings → 7 root
   causes fixed, occupancy slot-release the flagship). Next build:
   the Phase-3 drain loop (§5's tick + wake + relay).
4. **Open maintainer decisions** (§9): claim-lease recovery is now
   ANSWERED by §10.S2 (no new lease — the shipped detached lease keyed on
   the computed run_id), and retryable(n) is unblocked (§10.S3 removes the
   S3 coupling). Typed resource asks, no item sharding, and per-repo queue
   remain open.
5. **§10's verifications are ALL DONE — nothing in this plan is
   unverified.** The S1 placement leg is BUILT
   (`state/placement_drift.py` + `standing_consent_status`'s
   `placement-changed` leg, 2026-07-29) and the full suite passed with
   zero failures — the conservative absent-disables rule made it purely
   additive on the consent corpus, as designed. The S4 `--link-dest`
   probe was RUN ON BOTH CLUSTERS (2026-07-29, table in §10.S4):
   correctness holds on all four filesystems; hardlink dedup works
   everywhere except CARC BeeGFS `/scratch1`, where rsync silently
   degrades to the design's stated copy fallback. S4's content-addressed
   trees are cleared to build whenever eager submit is scheduled.

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
  **SHIPPED 2026-07-29 as CHAIN-DISPATCH** (`ops/queue/chain.py`): the
  retiring run's OWN driver chains exactly one `queue-dispatch` tick at its
  terminal step — the repo's shipped self-chaining shape (harness-contract
  capability 3: "S2/S3/S4 detach, campaign reconcile self-chaining, the
  driver watchdog"), no daemon and no model in the path. Two driver terminal
  seats call it, because a queue-placed run can retire at either:
  `ops/campaign_run.py::campaign_run` on its SYNCHRONOUS path (the body the
  detached child re-enters — the driver `queue-dispatch` itself starts) and
  `_kernel/lifecycle/block_drive.py::_chain` at its `terminal` return (the
  driver the `queue-drain` plan relays per drivable item, so a post-park
  retirement wakes the queue too). Retirement is decided by the ONE
  `state/queue_occupancy.run_occupies` predicate — a terminal STEP is not a
  retired RUN (`run_timeout`'s jobs are still live) and a supersession is a
  retirement with no driver terminal at all. Fire-and-forget by contract: the
  hook runs AFTER the settlement is durable, never raises, and reports its
  outcome as a `queue_chain` disclosure on the driver's own result. Gated on
  one non-creating `stat` of the intake ledger, so a repo that never used the
  queue pays nothing; the cadence (A retires → B starts → B retires → …)
  terminates on a dry ledger because a dispatch with nothing to place starts
  nothing.
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
3. **Phase 3 — the workflow relay (product side SHIPPED 2026-07-29):** N
   run-loops + queue ticks in one plan; merged park queue;
   park-with-context enrichment. Shipped as
   `src/hpc_agent/slash_commands/workflows/queue-drain.js`: one
   `queue-status` relay per pass, the drivable set as a mechanical field
   check over its `items[]` projections (`dispatched ∧ ¬terminal ∧ ¬held ∧
   ¬superseded_by ∧ (¬parked ∨ greenlight_unadvanced)` — the caller's
   formula over published fields; the verb mints no `drivable` verdict, so
   there is one definition and the plan owns it), a `block-drive` loop per
   drivable item with chunked `wait-detached`, `drive_attempts >= n` held
   rather than driven (retryable(n), on the kernel's durable counter so the
   budget survives the pass that spent it), parks RECORDED and left for the
   human, then re-status. Nothing is carried across passes (S5), and a
   pass with nothing drivable costs exactly one status relay (§7
   relaunch-cheapness). Two shipped bugs in `campaign-run.js` were fixed
   in the same pass — `--experiment-dir` relayed to verbs that do not
   declare it (rc=2; also hit `campaign-recon`'s `net-triage` probe), and
   result fields read off the CLI envelope ROOT instead of its `data`
   member — and `tests/contracts/test_workflow_plan_commands.py` now
   parses every declared relay against the real argparse tree and pins the
   envelope unwrapping, so neither class can ship unexecuted again. One
   Phase-3 ask remains kernel-side and is optional: a pass ceiling on the
   status digest. The plan derives its ceiling from `total_items` today,
   which is correct because §7 R3 deliberately left capacity to the
   scheduler — a real capacity field would only ever narrow the pass.
4. **The wake edge — CHAIN-DISPATCH (SHIPPED 2026-07-29):** the last
   always-draining gap. Phase 3 gave the queue a drain LOOP; it did not give
   it an EVENT. A dispatched run retiring freed capacity that nothing acted
   on, so the next waiting ledger item sat until a human or a drain pass
   happened by. `ops/queue/chain.py` closes it at the two driver terminal
   seats (§5 above has the full shape). With it, the queue is
   always-draining without a daemon: enqueue wakes it (`campaign-refill`
   dispatches its own item), retirement wakes it (this), and a drain pass or
   a human remains the manual backstop — three independent triggers over one
   idempotent, lock-guarded, request-id-deduped actor.

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

## 8. Fable-sweep verdict (2026-07-29) — what WILL go wrong, confirmed

Six adversarial lenses over this design + the live kernel (48 raw findings,
merged below; load-bearing code citations spot-checked against the tree;
two findings confirmed directly from the workflow engine's documented
semantics). **The v2 loop shape survives; the design was NOT buildable as
written.** Confirmed clusters, most severe first — each names its required
resolution. S1–S4 carried the blocking gate; **their resolutions are settled
in §10** and each entry below now points at its resolution. The findings
themselves are left unedited: they are the record of what was wrong, and
§10 is the record of what was decided.

**S1 — consent identity is placement-blind (HIGH).** §3 puts the cluster
inside the y, but both shipped identity tokens consumption compares are
provably cluster-free: run `cmd_sha` is "PURE PARAMETER identity"
(`state/runs.py` #207) and `_CAMPAIGN_IDENTITY_FIELDS` is
goal/budget/strategy/stop/anomaly (`meta/campaign/blocks.py`). Re-placement
to a cluster the consent never named passes the "spec-changed" leg.
RESOLUTION: fold placement into the consent-bound identity (a placement
field in the resolved spec that the consumption compare reads), and define
the campaign-scope analog before Phase 2.

**S2 — pre-dispatch has no claim, and "claims are hygiene" is false for
dispatch (HIGH).** The claim key `(run_id, driver_id)` cannot exist for an
intake item (no run_id until dispatch mints one), overlapping passes are
the design's own steady state, and dispatch (qsub) is not idempotent —
two dispatches can legally place on different clusters → distinct cids →
no dedup layer refuses, and a duplicated optuna trial pollutes `prior()`
history (wrong strategy decisions). RESOLUTION: claims are MANDATORY for
dispatch, keyed by intake `item_id` (minted at enqueue), with the E4
sequencing made a durable per-cid lock, not an in-process convention.

**S3 — refill-as-producer double-enqueues (HIGH).** `campaign-advance`
counts pool room over journal + sidecars; an enqueued-but-undispatched
item is in neither store, so every refill tick inside the enqueue→dispatch
window re-enqueues the same slots (and the direct-submit path is never
retired). RESOLUTION: advance's inputs must include queued intake items
for the cid (or refill marks intent in the sidecar store it already
reads); the v2 migration must explicitly retire refill's direct submit.

**S4 — the shared remote tree breaks eager submit (HIGH).** All runs of an
experiment on one cluster share one rsync'd code tree; integrity fires at
submit only. A job pending days executes whatever a LATER dispatch rsynced
— greenlit spec ≠ executed code, with false sha provenance. RESOLUTION
(pick one, before R3 ships): per-run remote snapshots (dir-per-run_id), or
a run-start cluster-side sha check against the sidecar that refuses to
execute on mismatch.

**S5 — resumeFromRunId across a park replays the park forever (HIGH,
FIXED 2026-07-29).** Engine cache replays unchanged (prompt, opts)
verbatim; the parked tick completed successfully, so a resumed run
returns the same park with zero live calls. Auto-resume is now FRESH
relaunch everywhere (plan meta, README, resume_hint) — cheap because
kernel state is durable. Chunk labels also now carry a chunk index so
identical wait chunks never collide in cache.

**S6 — the shipped wait relay could never survive a real wait (HIGH,
FIXED 2026-07-29).** The CLI's 7200s `wait-detached` default exceeded the
harness's ~10-min command bound: the relay was killed before it could
even report timeout → every real detached block parked `wait_failed`.
`campaign-run.js` now chunks (timeout_sec 480/chunk, maxWaitChunks≈12h,
heartbeat per chunk, `wait_stalled` park on exhaustion).

**S7 — pid/host-blind liveness, twice (HIGH).** `wait-detached`'s
`_live_lease` probes bare `pid_alive` — no host, no create_time — though
F43 added exactly those checks to the LAUNCH guard after a pid-reuse
wedge; lease files are never unlinked; the block-less glob
(`*-{run_id}`) can latch a suffix-colliding run or the always-alive
status-watch. The proposed claim-lease staleness probe re-specifies the
same hole. RESOLUTION: port F43 (host + create_time) into the wait path
and the claim lease; add lease cleanup; anchor the glob.

**S8 — post-y double-driver races (HIGH, FIXED 2026-07-29 — marker
consumption leg).** Inline-first-tick + auto-resumed pass = two drivers on
the same run at every y, by design. Pending-marker writes were blind
last-writer-wins (`_repark_marker` could resurrect a consumed park; a
loser's DetachedLeaseHeld becomes a spurious `tick_failed` park under
abort_on_failure; in-process spans have no lease at all). RESOLUTION as
stated — the marker clear/re-park is now a COMPARE-AND-SWAP on (boundary,
awaiting_since), inline-first-tick KEPT.

MECHANISM (landed): `state/journal.compare_and_clear_pending_decision` /
`compare_and_repark_pending_decision` — one locked-RMW definition each,
sharing `_swap_pending_decision` under the journal's existing per-run
`_locked` flock (the same critical-section precedent `stamp_drive_attempt`
and `upsert_run_compare_and_mint` use). `block_drive._consume_marker` is
the ONE seat both consumption legs (the greenlight resume in `run_tick`
and the standing-consent auto-advance in
`_consume_parked_boundary_under_consent`) clear through: it verifies the
marker on disk is still the `(block, awaiting_since)` pair the tick READ
before clearing it, so exactly one of two concurrent drivers runs the
successor span. The swap sits at the marker clear — the last read-only
point on the resume leg — so the LOSER writes nothing (no span, no
`_stamp_driver_tick`, no park), never re-parks the consumed decision, and
never raises: it returns `advanced` with a "another driver advanced this
boundary first" reason (`_lost_the_consume_race`). That classification is
deliberate: `skip` / `awaiting_decision` are the two outcomes §7's
`drive_attempts` charges as futile, and a lost race is not futility — the
chain DID move, so charging it would let concurrent drivers burn a healthy
item's retryable(n) budget; the drain's `advanced` branch re-ticks and the
next read shows the winner's position. `_repark_marker` (the F14
failed-span leg) now swaps only into an EMPTY slot, closing the
"resurrect a consumed park" half. Pinned by
`tests/_kernel/lifecycle/test_block_drive_greenlight_cas.py` (two
barrier-started threads on one real journal → exactly one span; the
sequential stale-marker refusal; the re-park resurrection guard).
STILL OPEN in S8: the loser's `DetachedLeaseHeld` → spurious `tick_failed`
park under abort_on_failure, and the missing lease on in-process spans —
neither is a marker-consumption defect.

**S9 — kill window downgrades a human's rerun to an advance (HIGH,
pre-existing kernel bug the queue multiplies).** SIGKILL between
`clear_pending_decision` and the resumed span leaves the y unconsumed and
the marker gone; the next tick's unscoped journal-derived path advances,
silently dropping the human's edit (F14 fix covers OSError only, not
process death). RESOLUTION: kernel fix — durable consume-intent record
written BEFORE the marker clear.

**S10 — two-store dispatch bookkeeping (MEDIUM).** Intake's `dispatched`
mirror duplicates the shipped `submitting` RunRecord organ; no atomic
write spans both, either crash order strands or duplicates, and no
reconciler is specified. RESOLUTION: intake keeps `queued`/`placed` only;
"dispatched" is a PROJECTION over the run stores (R1 applied to the
queue's own state), with the submit-once `submitting` record as the
handoff fact.

**S11 — scheduler-reality corrections to R3 (MEDIUM, several).**
`max_concurrent_jobs` is per-run wave grouping, NOT a cross-run account
cap (no organ enforces one; submit_plan lands ALL waves upfront as held
jobs); the pending-depth probe counts our own held waves (self-polluting
anti-affinity feedback); the 24h watch budget is shorter than realistic
pending (watch dies as 'timeout', wake never fires); clusters.yaml
carries contradictory walltime ceilings (3h constraints vs 24-48h
resolver); SGE cannot enforce the canary success-gate (completion-only
holds — unattended waves run after a failed canary); SGE Eqw (clearable)
is classified FAILED; scheduler rejection after the y has no re-placement
path and the F48 cross-cluster guard blocks the obvious manual recovery.
RESOLUTIONS: an account-level pending cap organ; probe excludes own held
waves; watch budget ≥ max expected pending or re-armable; reconcile the
two walltime fields; SGE canary becomes wait-then-submit (dispatch waits
canary terminal before wave submit); an Eqw 'scheduler-recoverable'
class; a rejected→re-place transition that requires a fresh y.

**S12 — projection discipline + O(history) (MEDIUM).** queue-status MUST
route through `is_committed_greenlight_for_boundary` (the bug-sweep-#1
lesson attention-queue already encodes) or the two "what needs me"
surfaces disagree; the append-only intake ledger + in-flock dedup scans +
never-called `prune_terminal_runs` + sidecar MAX_RUNS=500 rotation all
scale pass-startup with history, violating the relaunch-cheapness
invariant, and ledger items outlive their join targets. RESOLUTIONS: one
shared predicate module; intake compaction watermark; wire
`prune_terminal_runs`; define pruned-target semantics; `queue-run` mints
a client request_id consumed as the append dedup_key (replayed relays
double-enqueue otherwise).

**S13 — merged-park UX at depth (MEDIUM).** Return-when-nothing-drivable
at fleet scale = 15+ verbatim briefs at once (rule 2 forbids
summarizing), unordered (attention-queue's leverage ordering unused), a
relaunch per y. RESOLUTIONS: order the park queue via
`attention_queue.order_items`; batch the y-taking (one relaunch after the
sitting, not per y); parks carry pointers + the human reads renders from
disk (never workflow-summarized state).

## 9. Open questions for the maintainer

- Resource-ask vocabulary on intake items: free-form (opaque, policy reads
  known keys) vs typed (schema'd gpu/cores/walltime)? Typed proposed.
- Should an item be allowed to SPLIT (its task grid sharded across two
  clusters)? Proposed NO for v1 — an item is atomic; shard by enqueuing two
  items. Sharding one array across schedulers multiplies every failure mode.
- Cross-EXPERIMENT queue (one queue over many repos) — proposed NO: the
  queue is per-experiment-repo like every other store; a lab-level view is
  a reporting sweep, not a store.
## 10. Resolutions for S1–S4 (2026-07-29) — the pre-Phase-1 gate, cleared

Each resolution below was derived by reading the shipped mechanism rather
than by designing against the finding's summary. Two of the four findings
turned out to overstate what has to be built: S2's premise is false (the
run_id it says must be minted is already COMPUTED, purely), and S2/S3
collapse into ONE change. Line citations were spot-checked against the tree
at `dc4c872`.

### S1 — placement is a THIRD identity dimension, not a field in `cmd_sha`

**Confirmed as stated.** Both consent-bound tokens are provably
cluster-free: `compute_cmd_sha` (`state/run_sha.py:43`) hashes only the
materialized per-task kwargs — its docstring states "cmd_sha IS THE
PARAMETER IDENTITY OF THE EXPERIMENT", with executor and `tasks.py` bytes
deliberately excluded — and `_CAMPAIGN_IDENTITY_FIELDS`
(`meta/campaign/blocks.py:65`) is goal/budget/strategy/stop/anomaly.
`consume_boundary_under_consent` (`ops/overnight.py:935`) compares one of
them via `standing_consent_status`, so a re-placement passes the
spec-changed leg untouched.

**REJECTED: folding cluster into `cmd_sha`.** That token is also the dedup
key `find_run_by_cmd_sha` matches on. Folding placement in would make the
same experiment on two clusters two different experiments — killing dedup,
reproduction targeting, and §4's whole shared-study merge. The #207
boundary is load-bearing and stays.

**RESOLUTION — copy the code-identity precedent exactly.** Code has this
same problem and it was NOT solved by extending `cmd_sha`:
`state/code_drift.py` records `executor` + `tasks_py_sha` as a SEPARATE
dimension and compares them with a SEPARATE predicate
(`detect_code_drift`), precisely because cmd_sha is parameter identity.
Placement is the third dimension of that same kind.

1. The resolved spec gains an explicit `placement` block —
   `{cluster_key, ssh_target, remote_path, scheduler}` — resolved BEFORE
   the brief (§3's existing rule) and stamped on the run sidecar alongside
   `executor` / `tasks_py_sha`.
2. Consent binds the PAIR `(cmd_sha, placement_sha)`.
   `standing_consent_status` gains a placement leg returning a distinct
   `placement-changed` reason, so the park brief says which dimension moved.
3. **Conservative in the same direction as `detect_code_drift`:** an
   absent/empty recorded placement is NOT drift. Every pre-migration
   consent and sidecar predates the field; firing on absence would park
   every live consent at upgrade. This mirrors the module's own stated
   posture ("an empty/absent recorded value is NOT drift ... we cannot
   prove a pre-#351 record changed").
4. **Campaign scope uses MEMBERSHIP, not equality.** Do NOT add a cluster
   field to `_CAMPAIGN_IDENTITY_FIELDS`: a campaign spanning clusters is
   the entire point of §4, and an equality check would park on every
   placement swing, defeating dynamic split. The campaign consent instead
   carries a `placement_scope` — the SET of cluster keys authorized — and
   consumption checks membership. This is the campaign-scope analog S1
   asked for, and it is the natural on-ramp to §3's Phase-2 `{cluster:
   cap}` consent vocabulary (a scope that already names a cluster set is
   one field away from naming a cap per cluster).
5. Disclosure follows the shipped discipline: `overnight_consent.py:220`
   already requires the human's consent text to name the target sha prefix
   (`_names_target_sha_prefix`). An overnight consent must likewise name
   its cluster set out loud.

**BUILT + VERIFIED (2026-07-29).** The leg is shipped:
`state/placement_drift.py` (the one-home predicate, `code_drift`'s sibling),
a `placement-changed` leg in `standing_consent_status` ordered after
`spec-changed`, passthroughs in `consume_boundary_under_consent` /
`assert_standing_consent` / `block_gate.assert_greenlit_or_consented`, the
sidecar-fed `current_placement` at the run-scope consumption sites
(`submit_blocks` via `state/runs.read_run_cluster`, `block_drive`'s
successor-consume), and the §10.S1.5 disclosure in the overnight-consent
authorship gate (a placement-bound consent's chat grant must name every
cluster in the set; the coverage render shows the set). The blast-radius
question is MEASURED, not predicted: full suite green, zero failures — the
conservative rule held.

One refinement over the sketch above, decided at build time: the bound
identity is the CLUSTER-KEY SET compared by membership, not a
`placement_sha` over the full `{cluster_key, ssh_target, remote_path,
scheduler}` block. (a) a failing leg's reason must be legible in a park
brief ("consent bound to hoffman2, run now on carc") — a sha hides which
cluster; (b) `remote_path` legitimately varies WITHIN a cluster once S4's
content-addressed trees land, and a consent must not die because code was
re-uploaded to the cluster the human approved. The membership form also IS
the campaign `placement_scope` (point 4) — one predicate, both scopes,
already shipped; Phase 2 only has to start writing the list.

### S2 — the premise is false: run_id is COMPUTED, so the shipped lease already works

**The finding's core claim does not hold.** S2 states the claim key
"cannot exist for an intake item (no run_id until dispatch mints one)".
Run ids are not minted. `compute-run-id`
(`incorporation/build/compute_run_id.py`) is a `@primitive(verb="query",
idempotent=True)` with no side effects, and the derivation is pure:

    run_id = "<run_name>-<cmd_sha[:8]>"

Two workers looking at the same resolved item compute the IDENTICAL run_id.
The `(run_id, driver_id)` key is therefore available the moment an item is
resolved — which under §10.S3 is at ENQUEUE, not at dispatch.

**RESOLUTION — no refactor of the claim system. Reorder, don't rebuild.**

1. **Resolve at enqueue** (the same change §10.S3 requires), so run_id
   exists while the item sits on the ledger.
2. **Claim with the shipped lease, unchanged.** `_guard_single_lease`
   (`_kernel/lifecycle/detached.py:353–425`) is already the correct
   organ: flock'd, stamps `host` and `create_time`, and under F43 REFUSES
   a lease it cannot verify rather than reclaiming blind. Every one of
   those behaviours is a landed bug fix. Rewriting the claim system to key
   on a new `item_id` would re-earn all of them for no gain — the churn is
   not the main cost; losing the scars is.
3. **`item_id` still gets minted at enqueue**, but for a different and
   smaller job: it is the client `request_id` passed as
   `append_jsonl_line`'s `dedup_key` (`infra/io.py:310`, already shipped),
   which is what stops a replayed relay from double-enqueuing. That also
   discharges S12's replay hole with zero new code.
4. **E4 sequencing becomes a durable per-cid lock.** Today
   "strictly sequential, sidecar-between-slots" is an in-process loop
   convention (`ops/campaign_refill.py:26–35`); two passes break it. Use
   `advisory_flock` — the same primitive under `append_jsonl_line` and
   `ssh_slots` — held across resolve → sidecar write → spawn, exactly the
   window the refill docstring calls the crash window.
5. **A scheduler-visible idempotency token, and an adopt on recovery.** A
   claim cannot fix `qsub` non-idempotency: a dispatcher that dies after the
   scheduler accepts but before the record write leaves a live job no local
   store knows about. On any claim recovery, ADOPT such a job rather than
   resubmit. This leg is mandatory, and it is independent of the claim.

   **AMENDED AT BUILD TIME (Phase 2) — the carrier is NOT the job name.**
   This sketch originally said "stamp the item_id into the submitted JOB
   NAME". The shipped backend contract refuses that, and had already refused
   it: `infra/backends/_engine.py::build_correlation_flags` records that the
   correlation token rides a length-unconstrained scheduler CONTEXT/COMMENT
   field (Slurm `--comment`, SGE/UGE `-ac HPC_TOKEN=`) and is "NEVER put in
   `job_name` — SGE caps names at 15 chars and `job_name` is consumed
   byte-for-byte by log paths + canary naming (the whole reason OPEN-1(iii)
   was rejected)". Stamping an item_id there would silently break stderr log
   discovery (`_engine.py::stderr_log_path` interpolates `job_name`
   verbatim) on every SGE cluster. Phase 2 therefore left `job_name`
   untouched.

   What Phase 2 built instead: cluster-side identity keeps riding the
   existing correlation token `<run_id>#<attempt>`, and the ADOPT key is the
   LOCAL runs store — `ops/queue/dispatch.py::_adopted_status` checks for an
   existing `submitting` / `in_flight` / `complete` RunRecord under the
   COMPUTED run_id before starting anything, and reports `outcome="adopted"`
   with the status it found. This works because D2 makes run_id computed and
   deterministic, so two racers derive the same key without needing to ask
   the scheduler. The residual gap this sketch worried about — a job the
   scheduler accepted but no local store knows about — is covered by the
   pre-existing cluster-durable jobmap MARKER (`infra.jobmap`), which
   `build_correlation_flags` already names as "the authoritative id binding".
   No new scheduler query was added.

**Consequence for §9:** the open "claim lease TTL/steal policy" question is
answered by not existing — there is no new lease to write a policy for.

**Known collision, decide deliberately:** two DIFFERENT items with identical
resolved params and the same `run_name` compute the same run_id. For
campaign trials this cannot arise (params differ per trial). For
hand-enqueued items it is arguably correct (it IS the same experiment), but
`queue-status` must SAY so — "this item resolves to an already-claimed run"
— rather than silently collapsing two ledger rows.

### S3 — one shared "occupies a pool slot" predicate, and resolve at enqueue

**Confirmed, and the shipped code already names the exact hole.**
`ops/campaign_refill.py:19–57` documents that an orphan sidecar self-heals
through the BUDGET arm (it raises `spent_jobs`) but explicitly not through
the pool-room arm: "for a pool-room-bound refill under a generous
`max_jobs` budget the orphan does NOT shrink `pool_room` (K − in_flight)".
An enqueued-but-undispatched item is precisely that orphan, so every refill
tick inside the enqueue→dispatch window re-enqueues the same slots.

**RESOLUTION.**

1. **One shared predicate**, consumed by `campaign-advance` and
   `queue-advance` alike:

       occupied(cid) = journal status ∈ {in_flight, submitting}
                     ∪ intake items for cid in state {queued, placed}
       pool_room     = max(0, K − occupied(cid))

   This is R1 applied to the queue's own state: intake is not a second
   status store, it contributes exactly ONE fact no existing store holds —
   *this slot is committed to this cid and is not yet a run*. Three call
   sites currently count occupancy slightly differently; this makes it one
   definition (the same one-shared-predicate discipline S12 demands for
   `queue-status`).
2. **Refill's direct submit is retired**; refill becomes a ledger producer
   (`queue-run` and return), and the dispatcher is the only submitter.
   Note WHY this gets easier rather than being pure migration cost: E5's
   "resolve+submit per slot atomically, minimize the crash window" is a
   MITIGATION for having no durable record of intent. Once intake IS that
   record, the atomicity requirement genuinely relaxes.
3. **Resolve at enqueue, not at dispatch** — load-bearing, and the reason
   is subtler than S3 states. The async optuna scaffold indexes proposals
   by sidecar count (`optuna_async_strategy.py::_submitted_count` ==
   `len(prior_records(...))`) and CACHES per index. If enqueue happened
   before resolve, two dispatches of one item could resolve at different
   `_submitted_count`s and receive DIFFERENT trials — a worse bug than the
   double-enqueue being fixed. Resolving at enqueue consumes the index
   once, and it is also what makes §10.S1's placement-in-the-spec and
   §10.S2's computed run_id available while the item is still on the
   ledger.

**Accepted cost, stated once:** trial params are chosen at enqueue, so a
long-held item reflects the strategy's knowledge then, not at dispatch.
This is already true of every cached refill slot, and campaign-produced
queue depth is K-bounded — but the window widens for items held pre-gate,
and the morning brief should show enqueue age for that reason.

### S4 — content-addressed code trees; per-RUN copies are the wrong granularity

**Confirmed as stated.** `rsync_push` stage-swaps with `--delete` into ONE
`remote_path` per (cluster, experiment) (`infra/transport/__init__.py:415–460`);
drift is checked LOCALLY at submit (`state/code_drift.py`) and nothing
checks at job start. Under R3's eager submit a job pending for days executes
whatever a later push left in the tree, with a sidecar that claims
otherwise — a silent provenance lie, the worst failure class for this tool.

**The plan says "pick one" of per-run snapshots or a run-start sha check.
Picking one is wrong: the check is necessary but not sufficient, and the
snapshot should be keyed by CONTENT, not by run.**

1. **Run-start check (the guard) — ships first, one file.** At job start,
   recompute the code fingerprint over the tree about to execute and refuse
   to run on mismatch, writing a terminal marker with a distinct
   `code_drift_at_start` reason. The idiom already exists:
   `hpc_preamble.sh:343–400` writes `.hpc_failed/<run>.<task>.failed` and
   refuses to re-run. Cost is one hash per job. This converts a silent lie
   into a loud, classifiable failure — but it does NOT make the job
   succeed, and under eager submit every pending job would die on any push.
   So it cannot be the whole answer.
2. **Content-addressed trees — key by code version, not by run.** The
   system ALREADY computes a whole-tree content digest: `Manifest.digest`
   (`infra/manifest.py:68–88`), used today for the transfer delta, whose
   docstring invites exactly this — "two trees with identical file content
   ... produce the same digest, so a stage-in can dedup on it exactly the
   way `find_run_by_cmd_sha` dedups on the parameter sha". So:

       <remote_path>/.hpc/trees/<manifest digest>/

   N runs with unchanged code share ONE tree; a new tree appears only when
   the code actually changes. Per-RUN snapshots would duplicate the tree
   once per run for no added safety — the sharing that is dangerous is
   ACROSS RUNS OVER TIME, not across the tasks of one array.
3. **Pointing a run at it is one variable.** The job does `cd "$REPO_DIR"`
   (`hpc_preamble.sh:82`); `REPO_DIR` is already a per-run spec field
   derived from `remote_path`. Nothing else in the job path moves.
4. **This extends a ruling that is already settled, not a new one.**
   `--inplace` is deliberately banned (#F20,
   `infra/transport/__init__.py:1386, 1433`) because an in-place rewrite
   "tears the live dispatcher under a running array job". That is this
   exact hazard for RUNNING jobs; pending jobs are the same hazard with a
   longer fuse. Immutable per-version trees are the generalization.

**What this trades (honestly):**

| | cost |
|---|---|
| cluster disk | one tree per distinct code VERSION — bounded by edit frequency, not run count. Results are unaffected (already per-run dirs). |
| cleanup | REAL new bookkeeping: a tree is reapable once no non-terminal run references it. Mitigated by being the same janitor S12 already needs (`prune_terminal_runs` is wired, never called). |
| debuggability | "which tree did this run use?" becomes a recorded, rendered fact. Small but real. |
| transfer cost | **unchanged** — this is the cost usually assumed and it is not paid. |

**The `--link-dest` refinement — MEASURED on BOTH clusters (2026-07-29).
Nothing in §10 remains unverified.** Uploading into a fresh digest-named
directory would forfeit the delta (a new empty dir means a full transfer).
rsync's `--link-dest=<previous digest dir>` is built for exactly this:
unchanged files become hardlinks (zero bytes shipped, zero extra disk),
changed files are written fresh. Probe run by the maintainer on both login
nodes (rsync tree1 → `--link-dest` tree2 → replace file in tree1 → read
tree2):

| filesystem | dedup (hardlinked?) | preserve (old tree survives re-push?) |
|---|---|---|
| Hoffman2 `$HOME` | PASS | PASS |
| Hoffman2 `/u/scratch/...` | PASS | PASS |
| CARC `/home1` | PASS — same inode | PASS |
| CARC `/scratch1` (BeeGFS) | **FAIL — silently copied** | PASS |

Verdict: the CORRECTNESS property (a snapshot survives a later push) held on
ALL FOUR filesystems — the design is safe everywhere it will run. The
CHEAPNESS property is per-filesystem: Hoffman2 hardlinks everywhere (free
snapshots); CARC BeeGFS scratch refuses the hardlink and rsync degrades to a
full copy on its own, which IS the stated fallback — one tree copy per
distinct code version, bounded by edit frequency, never by run count. So the
implementation must NOT assume hardlinks: treat `--link-dest` as an
opportunistic optimization and (optionally) verify with a one-file inode
probe per (cluster, filesystem) at setup, recording the answer. S4's
content-addressed trees are cleared to build; eager submit's precondition is
now purely an implementation-ordering fact, not an open question.

**Ordering:** the guard is Phase 1-safe and can land immediately. The
content-addressed trees must land before R3's eager submit ships — with
them in place the guard becomes an assertion that essentially never fires,
which is the correct end state for a guard.

#### STATUS: BUILT (2026-07-29). Eager submit's precondition is met.

Shipped as designed above, with three implementation rulings the design did
not settle. Source of truth is the code + its tests, not this paragraph:
`infra/code_tree.py` (layout, identity, GC planner), the four transport dials
(`probe_code_tree` / `materialize_code_tree` / `seal_code_tree` /
`reap_code_trees`), `ops/submit_flow._deploy_code_tree` +
`_groom_code_trees`; pinned by `tests/infra/test_code_tree.py` and
`tests/ops/submit/test_code_tree_submit.py`.

1. **The tree holds CODE and symlinks — never a run's bytes.** §3 above said
   "nothing else in the job path moves", but the job path resolves
   `result_dir_template`, `${RESULT_DIR:-.}/.hpc_failed`, and the dispatcher's
   own sidecar RELATIVE TO CWD — so with `$REPO_DIR` on a tree they would all
   land INSIDE it, where the pull path cannot see them and the GC could delete
   them. (The trade table's "results are unaffected" was therefore too
   optimistic as written.) Fixed by symlinking every run-mutable path
   (`code_tree.TREE_SHARED_PATHS`) back to the base and excluding those paths
   from the snapshot, so nothing can materialise a real dir over one; the seal
   step VERIFIES each is a symlink (`[ -L … ]`) and refuses the tree otherwise.
   Only `REPO_DIR` moves — `remote_path` keeps every other meaning (submission
   cwd, `log_dir`, jobmap, results, pulls).
2. **The digest folds in the framework version.** `deploy_runtime` ships
   package-versioned framework files INTO the tree, so a tree keyed on user
   code alone would still let a queued job's dispatcher/preamble change
   underneath it. One digest = everything the job executes. `.hpc/runs/` is
   excluded from it, or every submission would mint its own tree (the per-RUN
   granularity this section rejects).
3. **The janitor's seat is the submit that MINTED a tree** — the same argument
   `ops/queue/maintenance` uses for grooming on the tick that lengthened the
   ledger. `probe_code_tree` already lists the trees root in the round-trip it
   uses to check the seal, so the PLAN costs zero extra network; a submit that
   REUSES a tree (the fast path) is charged nothing at all. Policy: reapable =
   unreferenced by any non-terminal run AND outside the newest N=3 AND not the
   digest being deployed. References are read off each `RunRecord`'s
   `job_env["REPO_DIR"]` — no new record field, and `None` for a pre-S4 run,
   which is also the migration story (absent-disables; a legacy run resolves to
   the base via `code_tree.repo_dir_for_run`). An unreadable journal REFUSES the
   whole pass rather than reading "no references" out of a failed scan.

`--link-dest` is wired exactly as the probe table demands: opportunistic, never
assumed, and the code cannot tell a hardlink from BeeGFS's silent copy. The
run-start code-identity check is untouched — defense in depth. Kill-switch:
`HPC_NO_CODE_TREES=1` restores the pre-S4 shape.

### What S1–S4 have in common

S1, S2, and S3 are one change wearing three hats: **write a few more facts
down at the moment an item joins the ledger** (its resolved spec, hence its
placement and its computed run_id; its `request_id`; and the fact that its
slot is spoken for). That is why the sweep insisted they be settled on
paper together — settled separately, they would have produced three
incompatible intake record shapes. S4 is genuinely independent and belongs
to eager submit, not to Phase 1.

