# Human amplification — the block architecture

**Status:** DRAFT — living design; the guiding principle of the hpc-copilot fork.
**Date:** 2026-07-03
**Origin:** 2026-07-02 fork decision. `hpc-copilot` forks `hpc-agent` at
`478729e7`. Upstream `hpc-agent` is preserved untouched as the
autonomous-research jumping-off point for a future where models are reliable
enough to hold it; this fork redesigns the same substrate for **human
amplification**.

---

## 1. Principle

Autonomous research is not attainable with current-generation models. The goal
of this module is to **amplify a human researcher**, not replace one:

- **Code** does all deterministic execution and all mechanical digestion
  (SSH, submission, polling, log harvest, metric extraction, failure
  classification).
- **The human** makes every decision.
- **The LLM** translates between them: it drafts proposals from code-digested
  evidence and digests the human's natural-language responses back into the
  next proposal. It never decides, never executes a transition past a decision
  point, and never interprets raw data.

Hard rule: **no decision point is ever resolved by the LLM.**

Scope rule: this module is an **experiment-agnostic HPC copilot**. Experiment
semantics — hypotheses, baselines-to-beat, outcome→conclusion keys — are out of
scope for core machinery. The most the core carries is a free-text run
description, requested by a one-off line of skill prose. Interpretation of
results enters exclusively through the propose loop (§2) at decision time; it
is never encoded as tool machinery.

## 2. The interaction primitive: propose → `y` / nudge

Every human touchpoint has one shape:

1. Code harvests and digests the evidence (status, errors+logs, metrics).
2. The LLM drafts a **proposal** over that digested evidence:
   - a debugging fix, when a block ended in failure;
   - a set of interpretation options, when a block ended in results;
   - a next-block suggestion, always.
3. The human answers with a **single letter `y`** (greenlight) or a
   **natural-language nudge** ("no — hold walltime, halve the grid instead").
4. On a nudge, the LLM digests it, re-drafts, and re-presents. Loop until `y`.

Notes:
- **The next-block suggestion is computed, journaled, and enforced — never
  free-prose** (decided 2026-07-03). Each block's envelope carries a
  machine-computed `next_block` (verb + why + spec hint — the campaign
  driver's `_next_step_hint` pattern, generalized). The LLM surfaces it the
  way `/sync` gets proposed at the end of a work chunk; the human's `y`
  greenlights *that named verb*; the journaled decision records it; and the
  next block's precondition gate verifies (a) its predecessor's code-written
  artifacts and (b) that the latest journaled greenlight names *it*. A
  mis-sequenced call fails loudly. Prose never hardcodes a sequence — the
  affordance is removed, not documented ("a guard the LLM itself satisfies
  is not a guard").
- Palatable status rendering is **syntactic sugar** — nice, never load-bearing.
  The load-bearing surfaces are (a) error digestion → proposed fix and
  (b) code-extracted results → proposed interpretations.
- "Results are never interpreted raw by an LLM" has an existing enforcement
  precedent upstream (#355: reducers refuse to fabricate; SKILL prose forbids
  hand-computed metrics). The fork extends the same doctrine from *computing*
  results to *concluding* from them.
- Every `y`/nudge exchange is journaled: the decision record, not the chat
  scroll, is the source of truth for why a run took the shape it did.

## 3. The block grammar

A workflow is a chain of **blocks**:

- A **skill shrinks to a ~single-sentence invocation** of the block's start
  primitive.
- The block **chains deterministically** in code as far as code can go —
  step N directly calls step N+1; no LLM in the transition.
- The block **terminates at the first decision point** (or at completion).
  Termination emits code-digested evidence; the LLM drafts the proposal (§2);
  the human decides; the LLM suggests the next block.
- Anomalies are block terminators too (§5). An anomaly is never silently
  retried by the LLM.

The SSH and scheduler machinery **lives in code, not in the LLM or prose**:
ConnectTimeout, BatchMode, per-host throttling, batched status — the inherited
spine (#346) — are non-negotiable code paths. Bespoke one-off ssh by the agent
at the edges is acceptable; blocks should be complete enough that the agent
never reverse-engineers the machinery mid-run.

**Blocks never block the chat (decided 2026-07-03).** A block verb whose
wall-clock is scheduler-bound (canary wait, main-array watch, speculation)
returns *immediately* after spawning a durable detached worker
(`_kernel/lifecycle/detached.py`), handing back `{started, run_id, watch:
journal}`; the journal is the state, completion rides the tail-loop / doctor /
cluster-watcher, and detach survives session death (harness-level backgrounding
never did). Prose is never the mechanism that prevents a stall — the verb's
contract is.

The **invocation surface, enforcement, latency, and curated-tool** decisions
that follow from this (MCP-preferred but enforcement-in-the-verb-not-the-surface;
the CLI as invariant substrate; the warm in-process runner; the `next_block`-derived
curated catalog) are specified in [`block-drive.md`](block-drive.md) §6–§7 —
where the wave-4 code-driven chain that supersedes LLM-executed transitions also
lives (§9).

### Submit, decomposed

- **S1 — resolve:** preflight → detect entry point → walk ambiguities,
  accumulating **all** decision points in one pass (the envelope machinery,
  unchanged — one brief, not twenty questions). Old `apply-safe-defaults`
  output survives as the **pre-filled recommendation** inside the brief, never
  as a silent actor. Ends: full decision brief → `y`/nudge.
- **S2 — stage & canary:** persist interview → scaffold → sidecar → canary →
  cost estimate. Ends: "canary green, est. N core-hours" → `y`/nudge.
- **S3 — submit & watch:** full submit → monitor arms (§5). Runs unattended to
  terminal state or anomaly.
- **S4 — harvest:** guaranteed (§5) — code-extracted results table → proposed
  interpretations → `y`/nudge.

### Block parallelism (latency opportunity)

Blocks create an interleaving opportunity:

- **Speculative mechanics:** decision-independent work (staging rsync, env
  probes, wheel checks) runs while the human reviews a brief.
- **Speculative canary:** run the canary under the recommended defaults during
  S1 review; `y` unchanged → S2 is already done; nudge → canary re-runs.
  Cheap by design, so mis-speculation is bounded.
- **Cross-run pipelining:** blocks of different runs interleave freely (the
  journal is per-run).

Interleaving rules (decided 2026-07-03):

- **Pre-greenlight cluster-touch policy:** read-only probes, staging rsync,
  AND the speculative canary may all run before a `y`. Rationale: the canary
  is a single-task array; the cluster self-cleans and the operator sweeps it
  periodically, so a stale speculative canary is queue noise, not damage.
  Nothing beyond the canary — no main array — ever enters the queue before a
  greenlight.
- **Speculation budget:** at most **one** speculative canary in flight per
  pending brief. Submit-scope only — campaign ticks never speculate. No
  core-hour accounting needed at this bound.
- **Nudge-cancellation:** speculative work is **never cancelled** on a nudge.
  If the nudge changed the spec (cmd_sha differs from what the canary
  launched under), the stale canary drains naturally and its result is
  ignored; an unchanged spec keeps the canary result and S2 is already done.
  No kill machinery on this path.

## 4. Campaigns: greenlit spec, then fully asynchronous

The campaign **spec** — strategy, budget, arms, stop conditions, anomaly
policy — is drafted and greenlit (§2) **once, at campaign start**. That spec is
the complete contract:

- Execution is **fully asynchronous against the spec**: reconcile ticks
  self-chain while healthy; the strategy (TPE/optuna/pbt) chooses next batches
  deterministically. There is **no per-iteration human boundary**.
- Human touchpoints: spec greenlight (start) · anomaly briefs (exception) ·
  completion brief with interpretation options (end).

## 5. Execution, monitoring, recovery (one machine)

These are one recovery machine, not separate features:

**Hybrid monitor.** A cluster-side watcher (a job/cron on the cluster — it
survives the laptop) writes a status file; a light client-side supervisor reads
it cheaply over the throttled spine and notifies. Either side dying is **loud**.
Watcher form (decided 2026-07-03): an **install-time probe ladder**, never
encoded site policy — try user `crontab` → `scrontab` (Slurm) → a
self-resubmitting minimal watcher job → none available, in which case install
nothing and say so loudly in the install envelope ("no cluster-side watcher;
overnight blindness persists"). The watcher script is stdlib-only and
short-lived per firing (write `status.json`, check `last_read` staleness) —
inside every center's cron-use policy pattern.

**Session tail-loop.** While the chat session is live, the LLM spawns a loop
tailing the local supervisor's output — the human sees liveness without asking.
If the chat session dies, job output is recovered from the cluster afterward.

> **Decided (James, 2026-07-03):** session-death recovery rides the doctor.
> The OS-scheduled `doctor` scan (below) also detects **orphaned runs**
> (in_flight + stale `next_tick_due` + no live driver) and raises a
> notification carrying a drafted re-arm proposal; a successor session (or
> the human directly) answers `y`/nudge. One mechanism covers both stall and
> session death — no separate machinery. Job output is recovered from the
> cluster by the ordinary guaranteed harvest once re-armed (tick idempotency
> makes the re-arm lossless).

**Idempotent reconcile ticks.** The driver primitive. Durable state only
(journal + filesystem + study DB); each tick harvests → records → resubmits
actually-dead work → refills to K; safe to re-run; no double-submit; loud-fail
guards (a task slot accruing ≥2 resubmits campaign-wide → stop and surface,
matching the within-run auto-retry cap; manifest-overridable). Idempotency is
what
makes every recovery below trivial: **re-arming loses nothing**.

**Driver watchdog (dead-man's switch).** The driver itself must be watched:

1. Every tick stamps `last_tick_at` + `next_tick_due` in the journal (deadline
   computed from the cadence the tick itself chose).
2. Independent failure domains check the stamp:
   - in-session: the harness timer loop;
   - out-of-session: an **OS-scheduled task** (Task Scheduler / cron) running a
     deterministic `doctor` verb that scans live runs for missed deadlines and
     raises a notification. The watch-the-watcher recursion **bottoms out at
     the OS scheduler** — the one layer treated as boring and reliable.
   - inverse failure (client vanished entirely): the client stamps a
     `last_read` marker cluster-side; the cluster watcher alarms when it goes
     stale.
3. The watchdog **never restarts anything** (no decision points). It surfaces a
   drafted recovery proposal — "driver stalled since 04:20, state X, re-arm?" —
   for `y`/nudge. Detection is the watchdog's whole job; safe recovery is
   already guaranteed by tick idempotency.

**Guaranteed harvest.** Every terminal path — completion, anomaly, cap
overrun, partial kill — ends in a code-harvest of whatever exists (metrics +
error sweep). No path ends in silence.

**First-class task state & telemetry.** Promoted from convention to contract,
now that the need is proven:

- The journal must answer at all times: what is running where, what was killed,
  what changed since the human last looked.
- Kill semantics: request → journaled intent → verified against the scheduler →
  surfaced as "N requested, N confirmed gone".
- Tick telemetry legibility is lintable: every field labeled cumulative vs
  per-tick delta (the `told 0 · complete 39/40` confusion class).

**No watcher is load-bearing; reconcile is the backstop.** Because an
idempotent reconcile tick re-derives ground truth from the cluster (`squeue` +
on-disk results) on *any* invocation, correctness never depends on a watcher
running. Watchers only shrink the *detection-latency* window — how long until
someone notices a given failure — they do not protect state. So the number of
watchers is a preference, not a requirement: the cheap default is the
in-session tail-loop (chat alive) + the OS-scheduled `doctor` (chat dead,
client up), which bottom out cleanly at the OS scheduler; the cluster-side
watcher (client vanished — overnight laptop sleep) is a genuinely distinct
domain but its marginal value over reconstruct-on-wake is thin, so it stays
**opt-in**, never default. Do not add a fourth watcher; add a failure domain's
proactive alarm only when reconstruct-on-next-look is too slow for it.

## 6. What this kills (in the fork)

- The headless `claude -p` worker and invoker-auth machinery — there is no LLM
  inside execution to spawn. The #137 OAuth blocker dissolves rather than
  getting fixed.
- `apply-safe-defaults` as a silent actor (→ pre-filled recommendation, §3).
- The "no `[Y/n]` prompts — every choice point has a deterministic resolution"
  skill doctrine. Inverted: `y`/nudge is the norm; deterministic resolution
  survives only *inside* blocks, between decision points.
- Worker-prompt prose as a routing surface. Prose only starts blocks.

## 7. Evidence (harxhar-clean sessions, Jun 22 – Jul 2 2026)

Mined from the ad-hoc Claude-driven CARC/Hoffman2 sessions that motivated the
pivot.

What worked (keep, formalized above):
| Behavior | Where it lives now |
|---|---|
| Poll ends in code-harvested table + error sweep, never silence | §5 guaranteed harvest |
| Agent judgment at anomalies, chosen explicitly over headless survival ("driven by the claude cli in case it crashes and burns") | §2 propose loop at §5 anomaly terminators |
| Idempotent reconcile tick, loud-fail guards | §5, the driver primitive |
| Submit returns immediately; something watches; human pinged with digest | §5 hybrid monitor + tail-loop |

What broke (fixed by design, not vigilance):
| Failure | Root cause | Fix |
|---|---|---|
| Overnight tasks died, empty output, no alarm | client-side detached shells die with laptop sleep | §5 hybrid monitor + watchdog |
| fail2ban bans, `getsockname`, timeouts | per-tick ssh connections, no ConnectTimeout, hygiene lived in prose | §3 SSH machinery in code only |
| Silent driver stall, caught by a lucky stale cron | driver had no watchdog | §5 dead-man's switch |
| "I thought I killed 4?" / `told 0` confusion | task state and telemetry not first-class | §5 state & telemetry contract |

Non-problems (explicitly not designed around): palatable status formatting
(sugar); LLM heartbeat gaps between notifications (harness hiccups); bespoke
one-off ssh at the edges (fine when blocks are complete enough).

## 8. TODOs (status as of 2026-07-03)

- [x] Session-death recovery mechanics — **decided:** doctor surfaces orphans
      (§5).
- [x] Block interleaving rules — **decided:** speculative canary allowed
      pre-greenlight, budget = 1 per pending brief, nudges never cancel (§3).
- [x] `doctor` verb — **built** (`hpc-agent doctor`, detection-only).
      OS-scheduler installation — **built** as a separate opt-in verb
      `hpc-agent doctor-install` (`ops/recover/doctor_install.py`), never
      auto-installed: Windows `schtasks /SC MINUTE` vs POSIX `crontab` on a
      platform branch, idempotent marker keyed on `repo_hash`, `uninstall`,
      loud-fail; writes a durable `doctor.spec.json` and schedules
      `hpc-agent doctor --spec …`. Verified on the 2026-07-03 proving run
      (verb wired, 11 tests green). Residual (optional): a Windows probe
      ladder + loud `installed:false`/`probes` map to fully mirror
      `watcher-install`'s Rung-4 degradation, and lifting `arm.py`'s adaptive
      cadence in place of the fixed `interval_minutes`.
- [x] Decision-journal schema — **built:** `append-decision` /
      `read-decisions` over per-scope `decisions.jsonl`; the schema prose
      lives in `docs/primitives/append-decision.md` (one record per
      exchange, append-only, `y` sentinel vs nudge text).
- [x] Block decomposition of status/aggregate/campaign flows — **built** to
      the submit S1–S4 grain (status-snapshot/status-watch,
      aggregate-check/aggregate-run,
      campaign-greenlight/campaign-watch/campaign-complete).
- [ ] Which upstream surfaces to physically delete vs strand (§6) —
      **decided: strand now, delete later.** The worker is removed from
      default routing; physical deletion happens in one dedicated pass once
      the blocks are proven on a real run. The skill-prose rewrite to
      single-sentence block starts rides that pass.
      **Update (2026-07-03, blocks now proven on Hoffman2):** scoped the deletion;
      it is a *dependency-untangling* job, not a file-removal one, and is NOT
      cleanly feasible in a single pass yet. The worker is confirmed fully
      stranded (every SKILL routes through `block-drive`; no SKILL emits
      `hpc-agent run`), so stranding carries no correctness risk. Clean-delete
      blockers: (1) `_kernel/lifecycle/drive.py` co-mingles the worker resolver
      (`default_judgement_resolver → run_workflow`) with the block watchdog
      stamp (`_stamp_driver_tick`, which `block_drive` imports); (2)
      `_wire/spawn_contract.py` (`WorkerReport` / `WORKFLOW_PROCEDURES` /
      `SpawnRequest`) is still imported by the campaign driver, the `describe`
      verb, `load-context`, and `llm_resolver`; (3) `cli/spawn.py::cmd_run`
      hosts the KEEP `--detached` status entry alongside the worker spawn; (4)
      `load-context` can still emit `agent`-kind delegates the legacy
      `hpc-campaign-driver --allow-agent-steps` feeds to the worker. **Deletion
      sequence:** (a) cut the campaign agent-step path to `block-drive` so
      `default_judgement_resolver`/`run_workflow` lose their last caller; (b)
      stop `load-context` emitting agent-kind delegates + drop the
      `render_spawn_prompt` prefill; (c) migrate `describe`/`llm_resolver` off
      `spawn_contract`; (d) then delete `invoke.py`, `run.py`, the `run` verb,
      `spawn_prompt.py`, `worker_prompts/`, `spawn_contract.py`, the worker
      schemas, `hpc-worker.md`, and the §5 worker tests in one commit.

## 9. Wave 4 — the code-driven chain

The first three waves left the LLM still *executing* the deterministic
block→block transition (it reads `next_block` and calls the next verb), which
contradicts §1 and bloats the loop. The next deliberate step moves the
sequencing into code — a stateless `block-drive` tick that chains the
deterministic spans and pauses at decision points, with the LLM collapsed to a
translator that renders briefs and commits an approved spec (never a `y`/nudge
sentiment the code parses). It also consolidates every design decision taken
since wave 3 (MCP-vs-CLI, curated surface, watchers, surface consolidation).

**Full spec:** [`block-drive.md`](block-drive.md). Gated on the proving run; a
refactor, not a tweak. Nothing from waves 1–3 is deleted by it (the §6 worker
deletion remains its own separate pass).

## 10. Spec review — separating syntax from logic

The propose loop (§2) surfaces a proposal for `y`/nudge, but leaves open *what the
human actually reviews*. The next design layer applies §1's division of labor to
the review surface itself: **the human reviews only logic; code owns all syntax.**
The LLM never authors a spec (it emits a flat *intent bag*; code builds and
validates); validators return typed outcomes so the L2/L3 line is a result-shape,
not a policy table; every human decision is a **code-enumerated `{choice}`** (the
LLM relays, never frames); and — the load-bearing correction — the human is asked
by a **consequence gate** (blast radius, not spec-cleanliness), not on every
transition, because a verification layer that over-fires gets turned off and
protects nothing.

**Full spec:** [`spec-review-separation.md`](spec-review-separation.md). Also gated
on the proving run — its sharpest open seams (render altitude, gate calibration,
per-family intent-schema authority) need a real researcher, not more design.
