# End-to-end submit sequence

What happens between a user typing `/submit-hpc` and their results
landing in `aggregated.json`. This walkthrough traces the full
pipeline so you can see how the layers interact in practice.

The load-bearing shift this page tracks: **the LLM no longer executes
the workflow.** There is no `claude -p --bare` worker reading a rendered
`worker_prompts/submit.md` procedure — that transport and its prompt
directory were removed (`docs/design/history/proving-run-2-hardening.md`
§6). The submit flow is four *human-amplification blocks* — code
primitives that chain in code and terminate at a human decision point
carrying a code-digested *brief* — driven by the stateless `block-drive`
tick. The seams to read alongside this page:

- `_kernel/lifecycle/block_drive.py::run_tick` — the tick that chains
  deterministic spans and parks at each decision.
- `ops/submit_blocks.py` — the `submit-s1`..`submit-s4` block bodies.
- `_kernel/lifecycle/detached.py::launch_submit_block_detached` — the
  detach-by-contract launcher for cluster-bound blocks.
- `infra/block_chain.py::ORDER` / `::GATED_BLOCKS` — the chain order and
  which boundaries require a human greenlight.

## The high-level shape

```
User chat            The slash            The skill relay        block-drive tick        Detached hpc-agent child
─────────            ─────────            ──────────────         ────────────────        ────────────────────────

/submit-hpc ──→ parse $ARGUMENTS
                Skill(hpc-submit, spec) ──→ invoke block-drive ──→ run_tick
                                                                     submit-s1 (resolve)
                                            ←── brief ────────────── parks: greenlight gate
                ←── relay brief
   ←── proposal
   y / nudge ──→ append-decision  ────────→ invoke block-drive ──→ run_tick
                (commit resolved)                                    submit-s2 (stage+canary)
                                                                     detaches ──────────────→ hpc-agent submit-s2
                                            ←── DetachedLaunch ──────                          owns SSH poll to terminal
                                            wait-detached (journal)                            stamps journal as it goes
                                            ←── worker_exited ──────────────────────────────── writes terminal record
                                            invoke block-drive ──→ run_tick
                                                                     replays s2 terminal
                                            ←── brief ────────────── parks: submit-s3 gate
   ←── relay brief                                                   … s3 (watch) … s4 (harvest)
```

The rendezvous points (where the tick parks and the human answers) are
the ONLY places an LLM touches the flow. Everything between them is code.

## Step 0: User intent

The user types something like:

```
/submit-hpc run ridge with horizon=[1, 5, 25]
```

Or an autonomous caller (a MARs experiment-runner) invokes the
`hpc-submit` skill directly. Either way the loop below is identical; the
only difference is who types the `y`/nudge (see [Variants](#variants)).

## Step 1: Slash body executes (interview wrapper)

The slash command body
(`src/hpc_agent/slash_commands/commands/submit-hpc.md`) is the
human-interview wrapper. It:

1. **Parses `$ARGUMENTS`** into an initial spec dict — whatever the user
   pre-stated (cluster, `no_canary`, `campaign_id`, and a
   `task_generator` if inferable).

2. **Invokes the `hpc-submit` skill** via the Skill tool with that spec.
   The skill body
   (`src/hpc_agent/slash_commands/skills/hpc-submit/SKILL.md`) is inlined
   into the agent's context; the agent now runs the skill's relay loop.

The slash never enumerates every field or fires blocks itself — the
skill owns the block invocation, and `block-drive` owns the sequencing.

## Step 2: The skill relays the block-drive loop (translation layer)

The skill's entire job is **translation at the rendezvous**: render each
brief as a proposal, take the human's `y`/nudge, and commit the approved
input spec. It never resolves a decision and never interprets raw
results (the #355 doctrine, extended from *computing* to *concluding*).
The loop, per `SKILL.md` "The driver loop":

### Step 2.1: Invoke `block-drive`

```bash
hpc-agent block-drive --experiment-dir . [--run-id <id>] [--workflow submit]
```

The first call starts the `submit` chain at its first block
(`block_chain.ORDER["submit"][0]` == `submit-s1`);
`block_drive.run_tick` chains the deterministic spans in code
(`block_drive._chain`) until it parks. A later call routes on the
approved spec (Step 2.4) and advances.

### Step 2.2: Render the brief the tick returns

The parked `BlockDriveResult` carries the block's code-digested `brief`
plus a code-rendered `relay` line. The skill relays `relay` verbatim and
surfaces the brief — never re-computing or re-interpreting its numbers.
Briefs by block: `submit-s1` the resolved plan with each ambiguity's
`safe_default` as a **pre-filled recommendation** (never auto-applied —
`apply-safe-defaults` is dead as a silent actor); `submit-s2` "canary
green, est N core-hours"; `submit-s3` the terminal status digest;
`submit-s4` a code-extracted results table plus an empty
`proposed_interpretations` slot for the human.

### Step 2.3: The human answers `y` or nudges

A single `y` approves the proposed input spec. Anything else is a
natural-language nudge ("use hoffman2 instead", "halve the grid").
**The code never reads the nudge string** — on a spec-changing nudge the
skill extracts the field delta and calls `revise-resolved`, which
re-resolves the journaled `resolved` (re-deriving `job_env`, `run_id`,
`cmd_sha`, the executor dispatcher). The delta names only INPUT fields,
so a derived-field corruption is impossible by construction.

`goal` and `task_generator` are human-authored: when either surfaces as
required, the skill asks and waits — it never proposes a sweep recipe.

### Step 2.4: On `y`, commit the approved spec, then tick again

```bash
hpc-agent append-decision --spec <path> --experiment-dir .
```

The record carries `scope_kind: "run"`, `scope_id: <run_id>` (or the
literal `pre-run` at the pre-resolve S1 boundary, before a `run_id` is
minted), `response: "y"`, and the approved input spec under `resolved`.
**The commit is the approval** (design §3/§5): the block-gate
(`ops/block_gate.py::assert_greenlit_target`) and the driver read
exactly this `resolved` spec — a spec, never the nudge text.

The next `block-drive` tick then routes deterministically. Given a
committed greenlight, `block_drive.plan_block_action` picks the action by
**identity + ownership** (design §4), never by sentiment:

- unchanged spec (`_spec_sha` matches) → **advance** to the stored
  successor;
- a changed field the current block owns → **rerun** it under the edit;
- changed fields owned strictly downstream → **advance_carrying** the
  edit into the successor.

Field ownership is `ops/field_ownership.py::route`. Until a `y` targeting
the parked boundary is committed, the tick reports `awaiting_decision`
and exits 0 (`block_drive._boundary_scoped_committed_resolved` scopes the
greenlight to THIS boundary so a prior boundary's consumed `y` cannot
masquerade as this one).

## Step 3: The blocks are the execution (no LLM inside)

Each block is a THIN orchestrator (`ops/submit_blocks.py`) that composes
existing rings and terminates at the first human decision point. There is
no worker to hand off to — the block body does the plumbing itself:

- **`submit-s1` (resolve)** — `submit-preflight` + `walk-submit-ambiguities`;
  on a clean walk with resolve inputs, chains `resolve-submit-inputs`
  (which builds `src/` via the content-hash `export-package` cache, mints
  the `run_id`, writes the sidecar) to a `resolved` terminator.
- **`submit-s2` (stage & canary)** — `submit-and-verify` stopped after a
  verified canary (task 0 ahead of the array) + the
  `estimate-core-hours` footprint.
- **`submit-s3` (submit & watch)** — the Phase-2 main-array launch
  (rsync + `qsub`/`sbatch`, idempotent on `cmd_sha`) + `monitor-flow` to
  terminal/anomaly + `decide-monitor-arm`.
- **`submit-s4` (harvest)** — `aggregate-flow` → the code-extracted
  results table.

### Greenlight gates and parking

`submit-s2`, `submit-s3`, `submit-s4` (and `aggregate-run`) are
greenlight-gated: their bodies call
`block_gate.assert_greenlit_target`, and the SoT set is
`block_chain.GATED_BLOCKS`. Because an in-code chain never journals the
human `y` a gate requires, `block_drive._chain` **parks before entering a
gated successor** (`block_chain.is_gated`), surfaces the predecessor's
brief, and exits — exactly as a `needs_decision` terminator does. The
next tick advances into the gated block once the human's greenlight is
journaled. `submit-s1`, the `status-*`, and `campaign-*` blocks are
ungated and chain in code without a park.

## Step 4: Detach-by-contract for cluster-bound blocks

The blocks whose wall-clock is cluster-bound never block the chat. When
a gated block is greenlit and would sit on an SSH poll, it detaches: the
parent verb runs its synchronous gate + drift guards, forces the spec's
`detach` field OFF, and
`_kernel/lifecycle/detached.py::launch_submit_block_detached` spawns a
**DETACHED `hpc-agent <verb>` subprocess** — *not* a `claude -p` worker —
running the SAME verb body. The child owns the SSH poll to terminal,
stamping the journal as it goes; the parent returns a `DetachedLaunch`
handle immediately.

The detach-supported verbs are
`detached.SUPPORTED_DETACHED_BLOCK_VERBS` (`submit-s2`, `submit-s3`,
`submit-s4`, `submit-speculate`, plus the status/aggregate/campaign
watch verbs). The child uses `start_new_session` (POSIX) /
`DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP|CREATE_BREAKAWAY_FROM_JOB`
(Windows) so it **outlives the orchestrator's session** — the crash that
killed the auto-backgrounded pipeline ~1s after `qsub` in 0.10.63 no
longer kills the poll. A filesystem lease keyed by `(run_id, block)`
(`detached._guard_single_lease`) refuses a second LIVE worker while
self-healing on a dead pid.

`block_drive._chain` recognizes the returned handle
(`block_drive._is_detached`) and exits with `action="detached"` — the
tick does not block.

## Step 5: The orchestrator learns the outcome by reading the journal

No LLM sits in the connection loop. The skill awaits the detached worker
via its harness backgrounding:

```bash
hpc-agent wait-detached --spec <path with {"run_id": ..., "block": ...}>
```

`wait-detached` (`ops/monitor/wait_detached.py`) blocks locally on the
worker's lease pid — no SSH — and exits the moment the worker does.
Programmatic callers poll `state/journal_poll.py::poll_until_terminal`
instead. On `worker_exited`, **the brief comes from one `block-drive`
tick, never from the worker's log**: the tick replays the finished
block's recorded terminal (`ops/submit_blocks._replay_recorded_terminal`,
keyed on the sidecar `cmd_sha` so a moved tree never replays a stale
outcome) and returns the code-digested brief. Composing a brief from
log-scraped numbers is the rule-10 relay-audit violation.

## Step 6: Time passes — the array runs on cluster

The user can disconnect; the cluster runs the array. Each task receives
`SLURM_ARRAY_TASK_ID` (or the SGE equivalent), loads `tasks.py`, calls
`resolve(task_id)` for its kwargs, calls the user's `@register_run`
function, writes a per-task metrics sidecar, and exits. Per-wave
combiners run as dependent jobs added during the submit-s3 `qsub` step.

## Step 7: Monitor and harvest

The submit chain itself watches (`submit-s3`) and harvests (`submit-s4`).
Standalone monitoring and aggregation are their own block chains,
driven by the SAME `block-drive` tick:

- `/monitor-hpc` → the `status` chain
  (`block_chain.ORDER["status"]` = `status-snapshot` → `status-watch`).
- `/aggregate-hpc` → the `aggregate` chain
  (`aggregate-check` → `aggregate-run`).

The `status-watch` and `aggregate-run` verbs are themselves detach-by-
contract, so an unattended cron tick never dials the cluster inline. The
aggregate harvest pulls `_combiner/` from cluster scratch, runs the
reducer (`execution/mapreduce/reduce/`) to produce `aggregated.json`,
ingests runtime samples into `runtime_priors/`, and advances the
sidecar's `lifecycle_state` to `aggregated`.

If a run completes with failures, the resubmit policy is applied via
`decide-resubmit`. The default threshold is 0: every failure escalates to
the human; auto-resubmit happens only when the caller opted in with
`resubmit_failed_threshold > 0` and the failed fraction is at or below
it.

## Variants

### Parallel startup (`/submit-hpc`)

The slash overlaps the block chain's startup latency with the human-
facing and local work: it fires `submit-speculate` (`ops/submit_speculate.py`)
when presenting the S1 brief — the default, not opt-in (design §3's
budget-1 speculative canary) — so the canary's queue+run time hides
inside the human's review and a plain `y` finds S2 already done. Nudges
never cancel it: a spec-changing nudge moves the `cmd_sha`, the stale
canary drains ignored, and the next canary is fresh. The one-per-brief
budget is enforced by the canary TTL cache; there is no kill path. See
[`../design/submit-parallel-canvass.md`](../design/submit-parallel-canvass.md).

### Campaign tick

A `/campaign-hpc` invocation drives the `campaign` chain
(`campaign-greenlight` → `campaign-watch` → `campaign-complete`). Unlike
submit, a campaign is **greenlit once at start** and then runs fully
asynchronously — reconcile ticks self-chain, the strategy picks batches
deterministically, and there is no per-iteration human boundary
(`meta/campaign/blocks.py`). See
[`campaign-lifecycle.md`](campaign-lifecycle.md).

### Anomaly recovery

`canary_failed` / `watching_anomaly` terminators are genuine human
branches with no single deterministic successor. The tick surfaces the
anomaly brief; the human's nudge names the recovery. A cluster-retarget
nudge ("try hoffman2") is one verb — `retarget-run` (selected by
`block_chain.recovery_arm_verb`) — which re-resolves on the new cluster,
supersedes the failed attempt, and re-canaries.

### Autonomous caller (MARs)

An autonomous agent invokes `Skill("hpc-submit", {...})` (or drives
`hpc-agent block-drive` directly) rather than typing `/submit-hpc`. It
plays the human's role at the rendezvous — reading the brief and
committing the `y`/nudge — but the driver, blocks, and detach mechanism
are identical. See
[`../workflows/code-driven-orchestration.md`](../workflows/code-driven-orchestration.md).

## What's NOT in the sequence

A few things sometimes confused with this flow:

- **There is no LLM inside a block.** The blocks are the whole execution;
  the LLM only translates at the parks. "Results are never computed by an
  LLM" (#355) extends to "conclusions are never drawn by an LLM".
- **The driver never reads a nudge string.** Routing is a function of the
  approved spec (identity + field ownership); the digestion of natural
  language into a spec happens in chat, before the commit.
- **The wave_map is internal to the submit stage.** It is not exposed to
  the human or the skill.
- **The DataAxis classification is mostly a no-op.** Most experiments are
  `Independent`; the matcher commits autonomously and the human never
  sees a data_axis dialog.

## See also

- [`../workflows/code-driven-orchestration.md`](../workflows/code-driven-orchestration.md) — driving the same substrate from a plain program
- [`../design/human-amplification-blocks.md`](../design/human-amplification-blocks.md) — why the flow is blocks-with-briefs
- [`../design/block-drive.md`](../design/block-drive.md) — the stateless resumable tick
- [`parallelization-axes.md`](parallelization-axes.md) — the five axes used during submission
- [`state-model.md`](state-model.md) — the state files this sequence reads and writes
- [`skill-policy.md`](skill-policy.md) — the layered architecture (interview / decision / execution)
- [`campaign-lifecycle.md`](campaign-lifecycle.md) — campaign-specific extensions to this sequence
</content>
