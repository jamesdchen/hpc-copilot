# Skill Policy — the block relay and the `y`/nudge norm

> **Fork inversion (2026-07-03).** This doc is rewritten for the
> **hpc-copilot** human-amplification fork
> ([`docs/design/human-amplification-blocks.md`](../design/human-amplification-blocks.md)).
> The old doctrine — "three layers, four surfaces; skills auto-resolve
> every choice point; **no `[Y/n]`**; every decision has a deterministic
> resolution" — is **inverted**. `y`/nudge over code-drafted briefs is now
> the norm; deterministic resolution survives only *inside a block*,
> between decision points. The headless worker is gone (design §6); prose
> only **starts blocks**.

hpc-agent ships markdown that LLMs read. Under the fork there are three
agent-facing responsibilities:

- **Code** does all deterministic execution *and* all mechanical
  digestion — SSH, submission, polling, log harvest, metric extraction,
  failure classification. This lives in the **block verbs**
  (`submit-s1`..`submit-s4`, `status-snapshot`/`status-watch`,
  `aggregate-check`/`aggregate-run`,
  `campaign-greenlight`/`campaign-watch`/`campaign-complete`, plus
  `submit-speculate`).
- **The human** makes every decision.
- **The LLM** (slash + workflow skill) **relays** between them: it renders
  a block's code-digested *brief*, collects the human's `y` (greenlight) or
  a natural-language nudge, journals the exchange, and invokes exactly the
  block the envelope's `next_block` named. It never resolves a decision
  point, never executes a transition past one, and never interprets raw
  data.

## The three surfaces

| Surface | What it does |
|---|---|
| **Slash commands** (`/submit-hpc`, …) | Human-interview wrapper: parse `$ARGUMENTS` into the initial spec, invoke the workflow skill, relay each brief and collect the user's `y`/nudge. |
| **Workflow skills** (`hpc-submit`, `hpc-status`, `hpc-aggregate`, `hpc-campaign`) | The block-loop relay: start the first block verb, then run the propose→`y`/nudge loop over each block's brief + `next_block`. Consumed via the Skill tool (interactive chat) or by direct read (another harness). |
| **Sub-skills** (`hpc-classify-axis`, `hpc-wrap-entry-point`, `hpc-build-executor`) | Finer-grained decision helpers, composed when a specific sub-decision is non-trivial. No paired slash. |

The old fourth surface — **worker prompts** — is **stranded** (see below).

## The forcing rule (inverted)

> **A slash and a skill *relay*; a block *executes and digests*; the human
> *decides*.**

- **The relay never resolves a decision.** A workflow skill starts a block,
  renders its brief, takes the human's `y`/nudge, journals it, and fires the
  block the envelope named. It does not auto-resolve the decision point, and
  it does not interpret the block's raw results (design §2). The old
  "skills auto-resolve every choice point; no `[Y/n]`" rule is dead: `[Y/n]`
  — as a single `y` greenlight or a nudge — **is** the interaction primitive.
- **Deterministic resolution survives only *inside* a block.** A block
  chains deterministically in code as far as code can go (step N calls step
  N+1, no LLM in the transition) and terminates at the first decision point.
  The plumbing branches inside a block (cache checks, lifecycle dispatch,
  retry-on-transient) are experiment-agnostic and stay in code — that is
  where "no prompts" still holds.
- **The next-block suggestion is computed, journaled, and enforced —
  never free-prose** (design §2). Each block's envelope carries a
  machine-computed `next_block` (`{verb, why, spec_hint}`). The LLM surfaces
  it the way `/sync` is proposed at the end of a work chunk; the human's `y`
  greenlights *that named verb*; the journaled decision records it
  (`resolved.next_block`); and the next block's precondition gate
  (`ops/block_gate.py`, `assert_greenlit_target`) verifies (a) its
  predecessor's code-written artifacts and (b) that the latest journaled
  greenlight names *it*. A mis-sequenced call fails loudly. **Prose never
  hardcodes the sequence** — the affordance is removed, not documented
  ("a guard the LLM itself satisfies is not a guard").
- **Every `y`/nudge is journaled** (`append-decision` /
  `read-decisions` over per-scope `decisions.jsonl`, design §2). The decision
  record — not the chat scroll — is the source of truth for why a run took
  its shape. Each nudge round is its own append-only record.

A **slash command's** body MUST:

- Parse `$ARGUMENTS` into the initial spec and invoke the matching workflow
  skill via the Skill tool. No `hpc-agent run` shell-out from the slash body
  (there is no worker to spawn any more).
- Relay each block's brief + `next_block` and collect the user's `y`/nudge.
  Carry NO workflow mechanics (no rsync, no qsub, no reduce prose) — the
  blocks own those.

A **workflow skill's** body MUST:

- Start the first block verb, then run the `y`/nudge loop: render the brief,
  collect `y`/nudge, `append-decision`, and on `y` invoke exactly
  `next_block.verb` (never a hardcoded chain, never a verb the envelope did
  not name) — until a terminal block.
- Prefer the MCP surface (`hpc-agent mcp-serve` typed tools); fall back to a
  single `hpc-agent <block-verb> --spec <path>` CLI call per block, and in
  the CLI fallback use the harness's native backgrounding for detached blocks
  — never a shell `&`.
- Never resolve a decision point and never interpret a block's raw results
  (the #355 doctrine extended from *computing* results to *concluding* from
  them).

A **sub-skill's** body MUST satisfy the same relay discipline at finer grain,
and returns to its parent via a file (`emit-skill-return` /
`fetch-skill-return`), not a chat message. Sub-skills have no paired slash.

## The decision table

```
                            │ RELAY                │ EXECUTION + DIGESTION │ DECISION
                            │ (slash + skill)      │ (block verbs, code)   │ (the human)
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Slash command (interview /  │ /submit-hpc          │ —                     │ —
   relay; human consumer)   │ /monitor-hpc         │   ← slashes relay     │   ← the user types the
                            │ /aggregate-hpc       │     briefs; they do    │     y / nudge
                            │ /campaign-hpc        │     not execute        │
                            │ /new-experiment-hpc  │                       │
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Workflow skill (block-loop  │ hpc-submit           │ (starts the block     │ —
   relay; paired w/ slash)  │ hpc-status           │  verbs; does not      │   ← the skill never
                            │ hpc-aggregate        │  resolve or interpret) │     resolves a decision
                            │ hpc-campaign         │                       │
                            │ hpc-notebook-audit   │                       │
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Block verb (code; execution │ —                    │ submit-s1..s4,        │ —
   + digestion; terminates  │   ← blocks are not    │ status-snapshot/watch, │   ← a block terminates
   at a decision point)     │     prose             │ aggregate-check/run,   │     AT a decision; the
                            │                      │ campaign-*,           │     human decides
                            │                      │ submit-speculate,     │
                            │                      │ notebook-lint/-view/  │
                            │                      │ -auto-clear/-status   │
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Worker prompt (execution    │ —  STRANDED (design §6): the headless `claude -p --bare` worker is removed
   layer, PRE-FORK)         │    from default routing — there is no LLM inside execution to spawn; the
                            │    blocks ARE the execution. The prompt files + spawn machinery stay on
                            │    disk untouched pending a dedicated deletion pass. Do not route to them.
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Setup (one-time, imperative │ hpc-agent setup      │ —                     │ —
   CLI command)             │   --cluster <name>   │                       │
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
CLI primitive (JSON-in,     │ —                    │ build-executor,       │ —
   JSON-out, no prompt)     │                      │ classify-axis, ...    │
```

## The frontmatter witness (still machine-enforced)

Every workflow skill statically declares two frontmatter fields the lint
cross-checks:

- **`execution`** — `inline` (runs in the main conversation) or `delegated`
  (pre-fork: inlined into a spawned worker). Under the fork **every workflow
  skill is `inline`** — it relays block briefs in-session; nothing is
  delegated to a worker.
- **`category`** — `agent-autonomous` (consumed via the Skill tool / direct
  read) or `worker-prompt` (inlined into a delegated worker). Must agree with
  `execution`: `inline` ↔ `agent-autonomous`; `delegated` ↔ `worker-prompt`.
  The `worker-prompt` category is now the **stranded** side of this mapping —
  no shipped skill declares it.

`scripts/lint_skill_command_sync.py` enforces the pairing (and that the
`inline` skills' paired slashes do **not** route through an `hpc-agent run`
spawn). See *See also*.

## Sub-skill composition still happens (outside the block loop)

The block verbs own the submit/status/aggregate/campaign mechanics, so the
workflow skills no longer compose sub-skills for those. But the sub-skills
(`hpc-classify-axis`, `hpc-wrap-entry-point`, `hpc-build-executor`) are still
reached directly — by the in-chat agent during onboarding, or by a slash's
setup phase — and their file-return seam and its harness hooks are unchanged;
they are documented below.

## A note on DataAxis (and what's NOT the privileged axis)

The framework has accumulated documentation prominence around the
DataAxis classification (`hpc-classify-axis` sub-skill, the four-way
taxonomy in `axis.py`, the matcher's pattern library). This can
mislead readers into thinking DataAxis is *the* central
parallelization concept in the framework. It's not.

The framework's privileged axis of parallelization is the user's
**sweep dimensions** (declared in `task_generator` via
`<experiment>/.hpc/tasks.py`). That's what produces the bulk of
parallelism: a user's `cartesian_product(seed=range(100),
model=["a","b"])` produces 200 tasks; the framework's task-array
machinery fans them out to the cluster. No DataAxis classification is
involved.

DataAxis matters only when a SINGLE task's `run()` function has an
inner loop you want to *further* chunk into sub-tasks. That's a niche
optimization. Most users never hit it because their sweep dimensions
provide enough parallelism.

The five parallelization axes — sweep dimensions, scheduling axis,
wave structure, stage DAG, DataAxis — are documented separately in
[`parallelization-axes.md`](parallelization-axes.md). Future
contributors should read that doc before treating DataAxis as a
central concern.

## The sub-skill return seam (and the autofetch hook)

A composed sub-skill (`hpc-classify-axis`, `hpc-build-executor`,
`hpc-wrap-entry-point`) returns to its parent via a **file**, not a chat
message: it writes its envelope to
`<experiment_dir>/.hpc/_returns/<skill>.json` (`emit-skill-return`), and
the parent reads it back (`fetch-skill-return`). This avoids the
end-of-turn signal that the Skill-tool chat-message return fires, which
stalls the parent mid-procedure. The set of skills that emit a return is
the single list `_KNOWN_SKILLS` in
[`hpc_agent/cli/skill_returns.py`](../../src/hpc_agent/cli/skill_returns.py).

There are three seams where the parent's *prose discipline* still matters:
remembering to `Skill(<sub>)`, remembering the follow-up
`fetch-skill-return`, and not ending the turn at the composition boundary.
The second and third are covered by harness hooks:

- **`skill-return autofetch` — a `PostToolUse` hook, matcher `Bash`.**
  [`hpc_agent/_kernel/hooks/skill_return_autofetch.py`](../../src/hpc_agent/_kernel/hooks/skill_return_autofetch.py)
  fires on the sub-skill's final `emit-skill-return` Bash call — the one
  event that coincides with the envelope existing. When the command invokes
  `emit-skill-return` for a skill in `_KNOWN_SKILLS`, it reads the committed
  envelope (`--experiment-dir` from the command, falling back to `cwd`) and
  injects it as `additionalContext`, so the return value lands in the
  agent's next observation whether or not the parent remembers to fetch it.
  It is **additive and fail-open**: it never deletes the file, and any
  non-`Bash` tool, non-emit command, unknown skill, missing/malformed file,
  or malformed payload is a clean no-op.

- **`skill-return stop guard` — a `Stop` hook.**
  [`hpc_agent/_kernel/hooks/skill_return_stop_guard.py`](../../src/hpc_agent/_kernel/hooks/skill_return_stop_guard.py)
  fires when the agent is about to end its turn. If a committed envelope
  for any `_KNOWN_SKILLS` skill sits unfetched under `<cwd>/.hpc/_returns/`,
  it blocks the stop with `{"decision": "block", "reason": …}` instructing
  the agent to `fetch-skill-return` and continue. Self-healing (the fetch
  deletes the envelope) and loop-safe (`stop_hook_active` passes through).

  *Both are harness-mediated, not `@primitive`s.* The agent never invokes
  them; the harness does.

**Install / disable.** `hpc-agent install-commands` (and `setup`) merge
both hooks into `~/.claude/settings.json` (`hooks.PostToolUse` +
`hooks.Stop`) — **additively and idempotently**. To **disable** either hook,
delete the entry whose `hooks[].command` contains
`hpc_agent._kernel.hooks.skill_return_autofetch` /
`…skill_return_stop_guard` from the corresponding array (a re-run of
`install-commands` re-adds it).

## See also

- [`adding-a-primitive.md`](adding-a-primitive.md) — the wire-surface recipe; complementary to this doc.
- [`sync-checklist.md`](sync-checklist.md) — invariants between slash-command surface and CLI.
- [`docs/design/human-amplification-blocks.md`](../design/human-amplification-blocks.md) — the fork's guiding design (block grammar, propose→`y`/nudge, what this kills).
- `scripts/lint_skill_command_sync.py` — enforces that every paired (slash, workflow skill) has both halves on disk and the slash routes to the matching skill via the Skill tool, that every sub-skill is in `SKILL_ONLY_OK`, and that every skill's `execution` + `category` frontmatter agree. The `category` field (`agent-autonomous` for skills consumed via the Skill tool / direct read; `worker-prompt`, now stranded, for skills inlined into delegated workers) is the machine-readable witness for this policy.
