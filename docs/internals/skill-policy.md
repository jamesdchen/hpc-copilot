# Skill Policy — three layers, four surfaces

hpc-agent ships markdown that LLMs read. The architecture is **three
layers** (interview → decision → execution) across **four surfaces**
(slashes, workflow skills, sub-skills, worker prompts). Each surface
has exactly one job; each consumer reaches into the layer it needs.

## The framing: three concerns, three layers

| Layer | What it does | Surfaces |
|---|---|---|
| **Interview** | Conducts propose-then-confirm dialogs with the human. Collects intent for any decision the decision layer can't auto-resolve. | Slash commands (`/submit-hpc`, etc.) |
| **Decision** | Resolves every choice point. Auto-resolves by default (autonomous mode); accepts caller-supplied values to skip resolution (interview mode); composes finer-grained sub-skills when a specific decision is non-trivial. | Workflow skills (`hpc-submit`, etc.) + sub-skills (`hpc-classify-axis`, etc.) |
| **Execution** | Runs the deterministic action sequence — rsync, qsub, journal write, canary, monitor loop, combiner. No decisions, no prompts. Reads the resolved spec, executes. | Worker prompts (`worker_prompts/<workflow>.md`) |

The flow is layered top to bottom:

```
Human types /submit-hpc                MARs experiment-runner
   ↓                                      ↓
INTERVIEW: slash collects decisions   (skip — agent has the spec)
   ↓                                      ↓
DECISION: workflow skill resolves remaining decisions
          (composes sub-skills for axis classification,
           entry-point onboarding, axes-init)
   ↓
EXECUTION: bare worker runs worker_prompts/<workflow>.md
          (deterministic sequence: rsync, qsub, canary, ...)
   ↓
envelope back up the stack
```

Two consumers (human, external agent) enter at different layers; both converge on the same execution layer with a fully-resolved spec.

## The forcing rule

> **Each layer has one job. A slash interviews. A skill decides. A worker prompt executes.**

The decisions made at each layer are also distinct:

- **Skills make experiment-aware decisions.** Which executor for *this* repo, which DataAxis for *this* run's loop, what walltime for *this* cmd_sha based on its runtime priors. The questions depend on the experiment's content.
- **Workers make experiment-agnostic decisions.** Is there already an in-flight run? Has this spec been cached in the journal? Did the canary succeed? Has the array been fully accepted by the scheduler? Workflow plumbing — the questions are the same regardless of what the experiment computes.

Both layers make decisions; both layers can branch. The split is *about what they decide on*, not whether they decide at all.

A **slash command's** body MUST:

- Conduct propose-then-confirm dialogs with the user for any decision the matching workflow skill needs.
- Invoke the matching workflow skill via the Skill tool with the user-resolved fields. No `hpc-agent run` shell-out from the slash body — that's the skill's job.
- Carry NO workflow mechanics (no rsync prose, no qsub, no journal). The skill is the canonical SoT for what the workflow does.

A **workflow skill's** body MUST:

- Take all inputs from the caller (slash or autonomous agent). No `[Y/n]`, no `Looks right?`.
- Walk every resolution step; **accumulate ambiguities into a single envelope, never early-return on the first miss.** When some fields can't auto-resolve, return `{ok: false, error_code: "needs_resolution", data: {resolved, ambiguities}}` with the full list. Each ambiguity carries `field`, `candidates`, `depends_on`, `safe_default`. The caller resolves every entry (slash walks user dialogs; autonomous caller applies safe_defaults) and re-invokes in one shot. Bounded by dependency DAG depth (~3 rounds max), not by ambiguity count.
- Compose sub-skills when a sub-decision is non-trivial (e.g., axis classification → `hpc-classify-axis`). Sub-skill ambiguities propagate upward into the workflow skill's `ambiguities` list.
- Hand off to the execution layer (`hpc-agent run <workflow>`) **only when the workflow has more than one LLM-driven step.** Single-step workflows (a one-shot status snapshot, a single-primitive query) should call the primitive directly — the bare-worker spawn buys no context isolation when there's nothing to isolate. Multi-step (submit, aggregate, campaign, blocking poll) hands off; intermediate tool calls accumulate in the worker's private context, not the caller's.

A **sub-skill's** body MUST satisfy the same rules as a workflow skill, just at finer grain. Sub-skills don't have paired slashes — users don't type `/classify-axis-hpc`; they reach sub-skills through a workflow skill's composition.

A **worker prompt's** body MUST:

- Be deterministic on the resolved spec — no LLM-judgment calls about the experiment's content (those happened in the skill).
- Plumbing-level branching is allowed and expected (cache checks, lifecycle dispatch, retry-on-transient-error) — these are experiment-agnostic decisions.
- No `[Y/n]`. Workers can't prompt the user (the bare worker has no Skill tool, no chat partner).
- Be eligible for prose hardening: snapshot tests on `cacheable_prefix` bytes, banned-hedging-phrase lints, primitive-reference cross-checks.
- Surface mid-flight ambiguities (e.g., co-tenant exclusion judgment) in the same `needs_resolution` envelope shape, with `safe_default` populated, so the calling skill propagates them up consistently.

## The decision table

```
                            │ INTERVIEW            │ DECISION              │ EXECUTION
                            │ (human-elicitation)  │ (agent-autonomous)    │ (deterministic action)
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Slash command (interview    │ /submit-hpc          │ —                     │ —
   layer; human consumer)   │ /monitor-hpc         │   ← slashes don't     │   ← slashes don't
                            │ /aggregate-hpc       │     decide; they      │     execute; they
                            │ /campaign-hpc        │     invoke skills     │     invoke skills
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Workflow skill (decision    │ —                    │ hpc-submit            │ —
   layer; paired w/ slash)  │   ← skills don't     │ hpc-status            │   ← skills don't
                            │     prompt; they     │ hpc-aggregate         │     execute; they
                            │     auto-resolve     │ hpc-campaign          │     run `hpc-agent run`
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Sub-skill (decision layer;  │ —                    │ hpc-classify-axis     │ —
   no paired slash;         │   ← composed by      │ hpc-wrap-entry-point  │   ← composed by
   composed by workflow     │     a workflow       │ hpc-build-executor    │     a workflow
   skills)                  │     skill, not       │                       │     skill, not
                            │     called directly  │                       │     called directly
                            │     by users         │                       │
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Worker prompt (execution    │ —                    │ —                     │ submit, status,
   layer; inlined into bare │   ← workers can't    │   ← workers don't     │ aggregate, campaign
   spawn worker)            │     prompt           │     decide; they      │
                            │                      │     execute resolved  │
                            │                      │     specs             │
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
Setup (one-time, imperative │ hpc-agent setup      │ —                     │ —
   CLI command)             │   --cluster <name>   │                       │
────────────────────────────┼──────────────────────┼───────────────────────┼─────────────────────────
CLI primitive (JSON-in,     │ —                    │ —                     │ build-executor,
   JSON-out, no prompt)     │                      │                       │ classify-axis,
                            │                      │                       │ submit-flow, ...
```

Structural empties confirm the layer rule:

1. **Slash decision/execution columns empty** — slashes are pure interview prose. They invoke skills; they don't decide or execute.
2. **Skill interview/execution columns empty** — skills are pure decision logic. They don't prompt the user; they don't run rsync.
3. **Worker prompt interview/decision columns empty** — workers are pure execution. They don't prompt (the bare worker has no Skill tool); they don't decide (every decision was resolved in the skill).
4. **Sub-skill interview/execution columns empty** — same rules as workflow skills, just composed rather than called directly.
5. **Setup decision/execution columns empty** — setup is one-time imperative environment authority. Anything per-submit belongs in a slash + skill pair, not setup.

## How this plays out in the current codebase

- **Workflow slashes** (`/submit-hpc`, `/monitor-hpc`, `/aggregate-hpc`, `/campaign-hpc`) — interview layer. Each conducts the propose-then-confirm dialog with the user for its workflow's decisions, then invokes the matching workflow skill via the Skill tool with the resolved fields. The slash body is pure interview prose; no workflow mechanics.
- **Workflow skills** (`hpc-submit`, `hpc-status`, `hpc-aggregate`, `hpc-campaign`) — decision layer. Each resolves missing fields autonomously (interview-mode callers pre-resolve via the slash; autonomous-mode callers like MARs's experiment-runner let the skill auto-resolve everything). Composes sub-skills for sub-decisions. Hands off to the execution layer via `hpc-agent run <workflow>`. No `[Y/n]` anywhere. The default hand-off forks a fresh-context `claude -p` worker; under the user-opt-in **inline transport** (`HPC_AGENT_INVOKER=inline`) no worker is forked, and the skill instead delegates the same rendered procedure to **a single in-session subagent when its harness exposes a subagent tool** (Claude Code's `Agent` tool, formerly `Task`) — recovering the worker's context isolation — or runs it in its own context when it has no such capability. The subagent path is capability-gated: a harness without one (a bare API caller, a notebook driver) falls back to in-context, never erroring on a tool it lacks. This does not move the spawn boundary of the policy above — inline is a transport choice the *user* opts into, and the subagent is the leaf that runs the deterministic procedure, not a new decision layer.
- **Sub-skills** (`hpc-classify-axis`, `hpc-wrap-entry-point`, `hpc-build-executor`) — decision layer, composed by workflow skills (and directly by the in-chat agent when the interview phase needs a specific decision). No paired slash — users don't type `/classify-axis-hpc`; they reach sub-skills through the workflow skill's composition. Same `[Y/n]`-free rule.
- **Worker prompts** (`submit`, `status`, `aggregate`, `campaign` under `src/hpc_agent/_kernel/extension/worker_prompts/<workflow>.md`) — execution layer. Inlined into the `claude -p --bare` worker prompt by `spawn_prompt._procedure_body`; the worker has no Skill tool. Hardening lives here: snapshot tests on the rendered `cacheable_prefix` bytes, banned-hedging-phrase lints, and `hpc-agent <primitive>` reference cross-checks.
- **Setup is a CLI step, not a slash.** Environment authority is one-time, imperative. `hpc-agent setup --cluster <name>` does the probe + cache marker; preflight check details carry actionable remediation prose so the primitive output is self-explanatory.

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

## When adding a new affordance

Ask, in order:

1. **Is this a one-time-per-machine concern?** If yes → setup. Stop.
2. **Is it mechanical given a JSON spec?** If yes → primitive. If a spawned worker should run it as part of a workflow, also add a worker prompt that calls the primitive.
3. **Is the new affordance a top-level user-typed workflow?** (Submit something. Monitor it. Aggregate. Drive a campaign.) Then it gets a (slash, workflow skill) pair under `WORKFLOW_PAIRS` and a `worker_prompts/<workflow>.md` for the execution.
4. **Is it a sub-decision composed into a workflow skill** (axis classification, entry-point onboarding, ...)? Then it's a sub-skill — no paired slash. List it in `SKILL_ONLY_OK`.

If the answer to (3) is "yes, for *only* the human consumer" — that's rare; usually it's a sign the work belongs in the slash body entirely, not in a skill.

## What this rule will not tell you

- It does not say a skill must be small or large. The constraint is consumer, not length. An agent-autonomous skill can have a long decision tree; what it can't have is `[Y/n]`.
- It does not say a skill cannot consult prior state or invoke other primitives. It can do anything mechanical. The constraint is just no synchronous prompting.
- It does not say a primitive cannot be backed by a skill. Most are. The skill is the *agent adapter* for the primitive — the primitive remains the harness-agnostic source of truth.

## The sub-skill return seam (and the autofetch hook)

A composed sub-skill (`hpc-classify-axis`, `hpc-build-executor`,
`hpc-wrap-entry-point`, `hpc-status`, `hpc-aggregate`) returns to its
parent via a **file**, not a chat message: it writes its envelope to
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
  event that coincides with the envelope existing. (It must NOT match the
  `Skill` tool: Claude Code's Skill tool returns *immediately* — its result
  is the injected instructions — so the sub-skill body, including the emit,
  runs *after* `PostToolUse(Skill)` fires; a Skill-matched hook can only
  ever see a stale envelope. The pre-0.10.58 version had exactly that bug
  and was a structural no-op.) When the command invokes `emit-skill-return`
  for a skill in `_KNOWN_SKILLS`, it reads the committed envelope
  (`--experiment-dir` from the command, falling back to `cwd`) and injects
  it as `additionalContext`, so the return value lands in the agent's next
  observation whether or not the parent remembers to fetch it. It is
  **additive and fail-open**: it never deletes the file (the manual
  `fetch-skill-return` prose keeps working), and any non-`Bash` tool,
  non-emit command, unknown skill, missing/malformed file, or malformed
  payload is a clean no-op — it can never block a tool call or crash the
  harness. The installed command wraps the Python entry in a bash `case`
  pre-filter so the every-Bash-call common path costs a bash builtin, not a
  ~300-500ms Windows interpreter start.

- **`skill-return stop guard` — a `Stop` hook.**
  [`hpc_agent/_kernel/hooks/skill_return_stop_guard.py`](../../src/hpc_agent/_kernel/hooks/skill_return_stop_guard.py)
  fires when the agent is about to end its turn. If a committed envelope
  for any `_KNOWN_SKILLS` skill sits unfetched under `<cwd>/.hpc/_returns/`,
  it blocks the stop with `{"decision": "block", "reason": …}` instructing
  the agent to `fetch-skill-return` and continue the parent's next step.
  This is the deterministic backstop for the advisory hand-back prose at
  sub-skill boundaries (empirical 2026-06-10: the sub-skill emitted its
  return and the turn ended anyway, stalling until a human typed "keep
  going"). Self-healing: the fetch deletes the envelope, so the guard has
  nothing left to block on; loop-safe: a payload carrying
  `stop_hook_active` (a stop that is already a hook-forced continuation)
  passes through.

  *Both are harness-mediated, not `@primitive`s.* The agent never invokes
  them; the harness does.

**Install / disable.** `hpc-agent install-commands` (and `setup`) merge
both hooks into `~/.claude/settings.json` (`hooks.PostToolUse` +
`hooks.Stop`) — **additively and idempotently** (a re-run does not
duplicate them, matched by module path so a moved venv — or a stale
pre-0.10.58 `matcher: "Skill"` entry — is healed in place; an existing
unparseable `settings.json` is left untouched and reported as
`skipped-unparseable`). The merge is
[`hpc_agent.agent_assets._merge_hook_entry`](../../src/hpc_agent/agent_assets.py);
its results are surfaced under the install envelope's
`data.settings_hook` (autofetch) and `data.settings_stop_hook` (guard). To
**disable** either hook, delete the entry whose `hooks[].command` contains
`hpc_agent._kernel.hooks.skill_return_autofetch` /
`…skill_return_stop_guard` from the corresponding array (a re-run of
`install-commands` will re-add it).

## See also

- [`adding-a-primitive.md`](adding-a-primitive.md) — the wire-surface recipe; complementary to this doc.
- [`sync-checklist.md`](sync-checklist.md) — invariants between slash-command surface and CLI.
- `scripts/lint_skill_command_sync.py` — enforces that every paired (slash, workflow skill) has both halves on disk and the slash routes to the matching skill via the Skill tool, that every sub-skill is in `SKILL_ONLY_OK`, and that every skill's `execution` + `category` frontmatter agree. The `category` field (`agent-autonomous` for skills consumed via the Skill tool / direct read; `worker-prompt` for skills inlined into delegated workers) is the machine-readable witness for this policy.
