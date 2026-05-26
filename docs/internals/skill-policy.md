# Skill Policy — when something earns a SKILL.md

hpc-agent ships markdown that LLMs read. There are **three places** it can live, and the choice is forced by the consumer.

## The framing: two consumers of hpc-agent

| Consumer | What they call | Decision-making |
|---|---|---|
| **Human** (Claude Code interactive chat) | `/<cmd>-hpc` slash command | The in-chat LLM elicits intent from the human, then invokes the skill with a resolved spec. |
| **Other agent** (MARs experiment agent, harness, batch driver, ...) | `SKILL.md` directly (via the `Skill` tool, or by reading the body and executing the same procedure) | The calling agent makes the decision autonomously — no human in the loop. |

The slash command is the human-elicitation surface. The skill is the agent-autonomous decision surface. **A skill does not interview the human.** When a human's intent is the source of truth (which YAML is the experiment identity, which entry point to onboard, whether a `BoundedHalo` halo expression is right), the slash command gathers that intent first and passes a fully-resolved spec to the skill. The skill then takes the deterministic path.

This is a flip from the prior framing ("human authority ↔ skill"). The prior framing assumed the only consumer of a skill was the user's chat. That's wrong: hpc-agent is also called by other agents (a MARs experiment agent, a notebook harness, a cron driver). Those callers have no `[Y/n]` channel, and a skill that prompts them is a skill they can't use.

## The forcing rule

> **Skill = agent-autonomous decision logic. Slash = human-elicitation wrapper around the skill.**

A skill's body MUST:

- Take all inputs from the caller (caller-supplied or detected from repo state). No `[Y/n]`, no `Looks right?`, no "Apply this edit?" prompts.
- Resolve every choice point deterministically. Ambiguity that cannot be resolved becomes a `spec_invalid` envelope (with a specific `error_code` like `ambiguous_entry_point` or `ambiguous_run`), not a prompt. The caller decides what to do.
- Have an explicit fail-safe default for heuristic decisions (e.g. `DataAxis` falls back to `Sequential` when the tree is ambiguous). The default must be the conservative-but-correct outcome.

A slash command's body MAY:

- Conduct propose-then-confirm dialogs with the user.
- Override the skill's autonomous defaults with user-confirmed choices (passed in via the spec).
- Skip skill invocation entirely when a state check resolves the request (e.g. cache hit, environment already configured).

## The decision table

```
                            │ human-elicitation                     │ agent-autonomous decision
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
Skill (in-chat or other-    │ —                                     │ hpc-build-executor
   agent consumer, Skill    │   ← empty by rule                     │ hpc-classify-axis
   tool or direct read)     │                                       │ hpc-wrap-entry-point
                            │                                       │
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
Slash command (human        │ /classify-axis-hpc                    │ —
   consumer, interactive    │ /wrap-entry-point-hpc                 │   ← empty by rule (a slash
   chat)                    │ /hpc-axes-init                        │     with no human elicitation
                            │                                       │     should be a primitive call)
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
Worker prompt (delegated    │ —                                     │ hpc-submit
   workflow, inlined into   │   ← empty by rule                     │ hpc-status
   spawn pipeline)          │                                       │ hpc-aggregate
                            │                                       │ hpc-campaign
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
Setup (one-time, imperative │ env probes, SSH agent,                │ —
   in /setup-hpc)           │ clusters.yaml validation,             │   ← empty by rule
                            │ experiment_roots config               │
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
CLI primitive (JSON-in,     │ —                                     │ build-executor, classify-axis,
   JSON-out, no prompt)     │   ← empty by rule                     │ interview, submit-flow,
                            │                                       │ monitor-flow, aggregate-flow,
                            │                                       │ combine-wave, ...
```

Structural empties confirm the rule:

1. **Skill-left empty** — a skill that interviews the human is a skill another agent cannot call. If you see one, the interview belongs in the paired slash command.
2. **Slash-right empty** — a slash with no human-elicitation content is a primitive call dressed up as a prompt. Either it elicits intent from the user, or it should be a thin trigger over `hpc-agent <verb>`.
3. **Worker-prompt-left empty** — spawned workers cannot actuate human authority. That's the escalation contract: workers handle only the deterministic column; human-elicitation needs flow back as escalations the user's in-chat Claude resolves via a slash.
4. **Setup-right empty** — setup is one-time imperative work. Anything that asks the user to re-affirm per submit belongs in a slash, not setup.

## How this plays out in the current codebase

- `hpc-build-executor`, `hpc-classify-axis`, `hpc-wrap-entry-point` — agent-autonomous decision logic. Each takes a partial spec, fills in the rest from repo inspection + heuristics, and invokes its underlying primitive. No `[Y/n]` anywhere in the bodies.
- `/classify-axis-hpc`, `/wrap-entry-point-hpc`, `/hpc-axes-init` — human-elicitation wrappers. Each conducts the propose-then-confirm dialog with the user, assembles a fully-resolved spec, and invokes the paired skill. The skill, seeing a complete spec, short-circuits its own elicitation paths.
- `submit`, `status`, `aggregate`, `campaign` — delegated execution. The text is **inlined** into the `claude -p --bare` worker prompt by `spawn_prompt._procedure_body`; the worker never invokes the Skill tool. These live at `src/hpc_agent/_kernel/extension/worker_prompts/<workflow>.md` (loaded via `importlib.resources`). Hardening that doesn't fit real skills lives here: snapshot tests on the rendered `cacheable_prefix` bytes, banned-hedging-phrase lints, and `hpc-agent <primitive>` reference cross-checks.
- **Preflight migrated to setup** — environment authority is one-time, imperative. `hpc-agent setup --cluster <name>` does the probe and writes the 24h cache marker `/submit-hpc`'s Step 6b gate reads.

## When adding a new affordance

Ask, in order:

1. **Is this a one-time-per-machine concern?** If yes → setup. Stop.
2. **Is it mechanical given a JSON spec?** If yes → primitive. If a spawned worker should run it as part of a workflow, also add a worker prompt that calls the primitive.
3. **Does it need a decision an agent (human or otherwise) makes per-experiment?** Then it's both a skill and a slash:
   - The **skill** encodes the decision-making logic — autonomous when called by another agent, accepting caller-supplied overrides when present.
   - The **slash** wraps the skill with human-elicitation prose. It runs in the user's chat, gathers intent via propose-then-confirm, and invokes the skill with the resolved spec.

If the answer to (3) is "yes, for *only* the human consumer" — that's rare; usually it's a sign the work belongs in the slash body entirely, not in a skill. If the answer is "yes, for *only* the agent consumer" — that's a skill with no paired slash, which is allowed but unusual (add it to `SLASH_ONLY_OK`-style explicit allow-listing in the lint).

## What this rule will not tell you

- It does not say a skill must be small or large. The constraint is consumer, not length. An agent-autonomous skill can have a long decision tree; what it can't have is `[Y/n]`.
- It does not say a skill cannot consult prior state or invoke other primitives. It can do anything mechanical. The constraint is just no synchronous prompting.
- It does not say a primitive cannot be backed by a skill. Most are. The skill is the *agent adapter* for the primitive — the primitive remains the harness-agnostic source of truth.

## See also

- [`adding-a-primitive.md`](adding-a-primitive.md) — the wire-surface recipe; complementary to this doc.
- [`sync-checklist.md`](sync-checklist.md) — invariants between slash-command surface and CLI.
- `scripts/lint_skill_command_sync.py` — enforces that every paired (skill, slash command) has both halves on disk and the slash routes to the right skill. The `category` frontmatter field (`agent-autonomous` for skills consumed via the Skill tool / direct read; `worker-prompt` for skills inlined into delegated workers) is the machine-readable witness for this policy.
