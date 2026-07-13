# Campaign lifecycle: from the armed-line slash command to the headless driver

Maintainer-facing design rationale for the campaign / headless shift. If
you're an integrator who just wants to *run* a campaign, start at
[`docs/workflows/campaign.md`](../workflows/campaign.md) — this document
explains *why* the surface looks the way it does today and what we tried
before settling there.

> **Update (2026-07-12): the driver was generalized and renamed.** This page
> was written when a campaign-specific `hpc-campaign-driver` console script
> (`meta/campaign/driver.py`) walked the loop by shelling `claude -p` for
> `kind: agent` steps. Both were **removed in the worker-removal wave**: the
> `claude -p` bare-worker spawn transport is gone, and the loop is now the
> block-chain driver `block-drive` (`hpc-block-drive` console script,
> `_kernel/lifecycle/block_drive.py`) over the neutral tick substrate
> `_kernel/lifecycle/drive.py`. Human-judgment steps are no longer spawned
> LLMs — they are **decision boundaries** where `block-drive` parks and a human
> (or the campaign skill relaying for one) answers. The one-step-per-invocation
> and state-on-disk invariants below are unchanged; only the *actor* changed.
> Present-tense claims about `hpc-campaign-driver` / `driver.py` / `claude -p`
> below are read as history — the pointers in the corrected spots and "See
> also" name the current reality.

## TL;DR

A campaign is a sequence of submit → monitor → aggregate → decide
iterations, each one optionally informed by the prior. The hard
question has never been *what* to run — it's *who drives the loop and
how do they recover when the driver dies between ticks*.

The current answer: the loop is driven by `block-drive` (the
`hpc-block-drive` console script, `_kernel/lifecycle/block_drive.py`), which
advances **exactly one step per invocation** by chaining blocks in code and
parking at each human decision boundary. (Its campaign-specific predecessor
`hpc-campaign-driver`, which walked a `delegate` block from `load-context` and
spawned `claude -p` for judgment steps, was removed in the worker-removal wave —
see the update banner above.) Driver crashes, session restarts, machine reboots
— none of them matter, because every byte of state needed to resume lives on
disk in `.hpc/`, and the driver reconstructs it from scratch each tick. Wrap it
in cron, in `/loop`, in a CI job, in nothing at all — the loop runs the same way.

Two prior shapes failed for reasons worth remembering before changing
the surface again.

## The journey

### Shape 1 — slash-command-in-a-conversation (deprecated)

The first working shape was: the operator runs `/submit-hpc
campaign_id=foo` in a Claude Code session, the slash command's skill
walks the operator through one iteration, the assistant decides the
next set of hyperparameters and fires another `/submit-hpc` itself.
"Concurrency is opt-in by firing more submits before earlier ones
finish" — see the still-accurate description in
[`docs/workflows/campaign.md`](../workflows/campaign.md).

What broke:

1. **Conversation = state.** The "campaign loop" lived in the
   conversation history. A context compaction, a `/clear`, or a session
   restart erased it. The operator had to re-derive the current
   iteration number, the last result, which experiments were in-flight.
2. **No way to walk overnight.** A 30-iter campaign that should finish
   in 12h needed the operator awake at every iter boundary. Real
   campaigns ran for days; we hand-waved this by saying "fire more
   submits in advance" but that's not a loop, it's pre-allocation.
3. **No external orchestrator path.** Anything outside Claude Code
   that wanted to drive the loop — cron, a CI job, an external agent
   — had to re-implement the iter logic from scratch.

### Shape 2 — armed-line + Stop hook (rejected and ripped out)

The first attempt to escape conversational state was an `armed: exit`
contract in the slash-command output, intercepted by a Claude Code
Stop hook that re-armed the next tick. The slash command would print a
JSON line like `{"armed": true, "next": "/monitor-hpc ..."}`; the Stop
hook parsed it, re-fired the next slash, and the loop walked itself
without operator input.

What broke:

1. **Hook-coupling to one harness.** The Stop hook only worked inside
   Claude Code. cron / external orchestrators couldn't participate.
2. **Brittle to message ordering.** A multi-line tool result, a
   trailing newline, an interleaved permission prompt — any of them
   could break the parse and the loop silently died.
3. **Per-installation install footprint.** Every operator had to
   install the hook (`bd5606d fix: pin monitor-armed Stop hook to the
   installing interpreter`), upgrade it when we changed the protocol,
   and trust that their CC version played well with it.

The whole subsystem was ripped out in commits
`6083979 refactor: remove /monitor-hpc armed: exit contract and Stop-hook subsystem`
and `eef9d07 docs: drop armed-contract/hook references and changelog the rip-out`.

### Shape 3 — load-context + delegate block + headless driver (current)

The two ideas that made the third shape work:

1. **`load-context` reconstructs run/campaign/cluster state purely
   from on-disk artifacts** (`cc63c84 feat: add load-context primitive
   for fresh-context workflow steps`). Any fresh process — a new
   Claude Code session, a cron tick, a different orchestrator — calls
   `hpc-agent load-context --experiment-dir .` and gets the same view
   the previous tick had. No conversational memory dependency.
2. **`load-context` also emits a `delegate` block describing the next
   step as a self-contained unit of work** (`471680e feat:
   precondition gates, delegate block, and headless campaign driver`).
   The block is one of two kinds:

   * `kind: "cli"` — a deterministic step (monitor, aggregate). The
     driver runs the named workflow atom directly. No LLM, no judgement.
   * `kind: "agent"` — a judgement step (a fresh submission with new
     hyperparameters, a `decide` step). The driver shells `claude -p`
     against a content-addressed spawn prompt that the framework
     renders deterministically from on-disk state. Pinned by hash so
     the same disk state always produces byte-identical worker input.

`hpc_agent.meta.campaign.driver` (the `hpc-campaign-driver` console script)
is **non-primitive** — it doesn't appear in `capabilities`, doesn't
emit the standard envelope, and isn't composable with the atom layer.
It is one of the only intentional non-primitives in the codebase, on
purpose: the driver is the *outer loop*, not a step within one. Making
it a primitive would invite recursion (driver-invoking-driver) without
benefit.

The loop *mechanism* is not campaign-owned, though. The generic
tick-loop — read a `delegate`, plan, dispatch `cli`/`agent`, one step
per invocation — lives in `_kernel/lifecycle/drive.py` as neutral
substrate: it knows nothing about campaigns and never imports
`meta.campaign`. `driver.py` is the campaign *caller* that configures
it, injecting a `StepTable` (the `monitor`/`aggregate` → flow-verb map)
and a `JudgementResolver` (the default `claude -p` path) through
`CampaignLoopConfig`. The mechanism owns the protocol; the caller owns
the policy — the same seam `_kernel/decision/kernel.py` establishes for
deterministic routers. That split is what lets a non-campaign sequence
reuse the loop without inheriting campaign vocabulary, and what scopes a
future non-Claude resolver (#305) to an injection rather than a fork.

Each tick of the driver:

1. Runs `load-context --experiment-dir .`.
2. Reads `data.delegate`. If absent → campaign is complete; exit.
3. Dispatches by `delegate.kind`:
   - `cli`: runs `hpc-agent <workflow> --spec <inline-spec.json>` and
     ingests the envelope.
   - `agent`: requires `--allow-agent-steps` (spawning an LLM is a
     billable side effect we make the operator opt into); renders the
     spawn prompt via `hpc_agent._kernel.extension.spawn_prompt`, runs `claude
     -p` through the `WorkerInvoker` transport seam, parses the
     structured `WorkerReport`.
4. Returns. **One step per invocation.** Cron / `/loop` / a bash loop
   handles cadence.

State carried between ticks is **only** what's on disk under
`.hpc/`: run sidecars, campaign manifest, cursor, runtime priors,
journal. The driver carries nothing in memory across ticks because
there is no "across ticks" — every tick is a fresh process.

## Subsystems that the third shape required

These exist because Shape 3 needs them; they're not free-standing
features:

| Subsystem | Why Shape 3 needs it |
|---|---|
| `load-context` primitive (`meta/campaign/atoms/load_context.py`) | Single source of truth across processes. |
| `delegate` block on load-context output | The driver's input contract. |
| `spawn_prompt` rendering (`_kernel/extension/spawn_prompt.py`) | Deterministic prompt for `kind: agent` steps; cache-hit by hash. |
| `WorkerInvoker` transport seam (`_kernel/lifecycle/invoke.py`) | Lets the driver swap `claude -p` for a mock in tests / for a different LLM transport in production. |
| `hpc-agent describe` (`d25cc40`) | Workers fetch primitive contracts per-branch instead of inheriting them from parent context. |
| Plugin worker-prompt resolution in `_procedure_body` (`01edb63`, renamed in `7a39b5e`) | Plugin overrides reach delegated workers, not just in-conversation skills. |
| `campaign_id` on the run sidecar v2 schema | Cross-iter linkage that the driver reads via `mapreduce.reduce.history.prior(...)`. |
| `validate-stochastic-marker` (`b61c309`) | Driver-enforced gate that catches cmd_sha collisions before they silently dedup. |

The order they landed is roughly the order they were forced:
load-context first, then the spawn pipeline, then the marker gate,
then plugin worker-prompt resolution. The README in `docs/internals/` lists
the current set without the history; this doc is the history.

## Failure modes the current shape still has

Honest list — the third shape isn't perfect, just better than the
first two.

1. **Cost visibility on `--allow-agent-steps`.** Each `kind: agent`
   tick spawns a `claude -p` worker. The flag is the only guardrail;
   there's no per-campaign budget cap on agent spawns yet. An
   operator who sets the cron interval too tight and forgets the flag
   gates can burn real money.
2. **Worker-prompt body must travel inside the spawn prompt.** Headless
   `claude -p --bare` doesn't have skill discovery, so
   `spawn_prompt._procedure_body` inlines
   `_kernel/extension/worker_prompts/<workflow>.md` verbatim. If a
   prompt grows past the model's prompt-cache sweet spot, the
   per-tick cost climbs.
3. **No driver self-watchdog.** If the driver process is killed
   mid-step (e.g. between firing `submit-flow` and writing the
   resulting run sidecar), the next tick has to detect the orphan
   via `find_in_flight_runs` rather than via a driver-side journal.
   The state-on-disk discipline mostly absorbs this, but it's load-bearing.
4. **Plugin worker prompts are first-write-wins.** `_procedure_body`
   picks the first plugin to provide a `worker_prompts/<name>.md`
   (via the `worker_prompt_assets` attribute); two plugins
   that both ship `/submit-hpc` would race on entry-point order.

## When to change the surface again

The campaign loop has moved twice; we should be slow to move it a
third time. Pre-conditions worth meeting before a Shape 4:

- The new shape preserves the **state-on-disk discipline**. No
  in-memory loop state, no conversation-coupled assumptions.
- The new shape preserves **one-step-per-invocation**. Anything else
  reintroduces "what happens when the loop driver dies mid-iter."
- The new shape preserves the **harness-agnostic contract**: cron, an
  external agent, a CI runner, and Claude Code can all drive the
  loop identically.

If those three constraints hold, the surface can change. If one of
them is at risk, the new shape is probably re-running an old mistake.

## See also

- [`docs/workflows/campaign.md`](../workflows/campaign.md) — user-facing
  "how do I run a campaign" guide.
- [`docs/workflows/memory-across-campaigns.md`](../workflows/memory-across-campaigns.md)
  — the `interview` ↔ `recall` loop that persists campaign intent
  across iterations.
- [`docs/primitives/load-context.md`](../primitives/load-context.md) —
  primitive contract.
- [`docs/architecture.md`](../architecture.md) — layering rules; the
  driver lives above flows, below the slash-command surface.
- `src/hpc_agent/_kernel/lifecycle/block_drive.py` — the current driver:
  `block-drive` (`hpc-block-drive` console script), which chains blocks in code
  and parks at each human decision boundary (`plan_block_action` / `run_tick`).
- `src/hpc_agent/_kernel/lifecycle/drive.py` — the neutral tick substrate
  `block_drive` generalizes; its `kind: agent` steps are now always planned as
  `skip` (a human decision boundary), the `claude -p` transport having been
  removed in the worker-removal wave.
- `src/hpc_agent/meta/campaign/blocks.py` — the three campaign touchpoint blocks
  (`campaign-greenlight` / `campaign-watch` / `campaign-complete`) that
  `block-drive` chains, plus the `watching_refill` hand-off to `campaign-refill`.
- `src/hpc_agent/ops/campaign_refill.py` — the autonomous refill actor the
  greenlit manifest authorizes (RFC #362; `docs/design/campaign-async-refill.md`).
- `src/hpc_agent/slash_commands/skills/hpc-campaign/SKILL.md` /
  `slash_commands/commands/campaign-hpc.md` — the agent-invoked skill and the
  user-typed slash that relay each `block-drive` decision brief.
  (The former `meta/campaign/driver.py` shim and the
  `_kernel/extension/worker_prompts/campaign.md` worker prompt were deleted in
  the worker-removal wave.)
