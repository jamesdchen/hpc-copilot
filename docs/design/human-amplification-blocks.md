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

### Block parallelism (latency opportunity — stub)

Blocks create an interleaving opportunity:

- **Speculative mechanics:** decision-independent work (staging rsync, env
  probes, wheel checks) runs while the human reviews a brief.
- **Speculative canary:** run the canary under the recommended defaults during
  S1 review; `y` unchanged → S2 is already done; nudge → canary re-runs.
  Cheap by design, so mis-speculation is bounded.
- **Cross-run pipelining:** blocks of different runs interleave freely (the
  journal is per-run).

TODO: interleaving rules — what may touch the cluster before a greenlight,
speculation budget caps, cancellation semantics on nudge.

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

**Session tail-loop.** While the chat session is live, the LLM spawns a loop
tailing the local supervisor's output — the human sees liveness without asking.
If the chat session dies, job output is recovered from the cluster afterward.

> **TODO (James):** exact session-death recovery mechanics — e.g. a successor
> session (or the OS-scheduled doctor, below) locates orphaned runs via the
> journal and re-arms tail + harvest from cluster state. Details deliberately
> open.

**Idempotent reconcile ticks.** The driver primitive. Durable state only
(journal + filesystem + study DB); each tick harvests → records → resubmits
actually-dead work → refills to K; safe to re-run; no double-submit; loud-fail
guards (same task resubmitted >2× → stop and surface). Idempotency is what
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

## 8. Open TODOs

- [ ] Session-death recovery mechanics (§5 stub — James).
- [ ] Block interleaving rules: pre-greenlight cluster-touch policy,
      speculation budgets, nudge-cancellation semantics (§3 stub).
- [ ] `doctor` verb spec + OS-scheduler installation story (§5).
- [ ] Decision-journal schema: what a recorded `y`/nudge exchange persists (§2).
- [ ] Block decomposition of status/aggregate/campaign flows to the same grain
      as submit S1–S4 (§3).
- [ ] Which upstream surfaces to physically delete vs strand (§6), and the
      skill-prose rewrite to single-sentence block starts.
