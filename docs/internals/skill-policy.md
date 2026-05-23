# Skill Policy — when something earns a SKILL.md

hpc-agent ships markdown that LLMs read. There are **three places** it
can live, and the choice is forced by the consumer. Get this wrong and
you either (a) write hand-wavy prose where a deterministic primitive
belongs, or (b) you ship a deterministic prompt with no test coverage
because it's masquerading as an LLM-tolerant skill. Both have happened.

## The axis: who consumes it

| Consumer | Mechanism | Drift tolerance |
|---|---|---|
| The user's interactive Claude Code chat | Real Skill tool, `~/.claude/skills/` discovery | High — the LLM interprets intent |
| A code-rendered worker prompt for `claude -p --bare` | Text inlined into the cacheable prefix by `spawn_prompt._skill_body` | Low — the prompt is deterministic, the model boundary is the only stochasticity |
| A primitive caller (any harness via the JSON CLI) | None — primitives have no prompt | Zero — pure function call |

## The forcing rule

> **Human authority required ↔ Skill. Mechanical-given-spec ↔ worker
> prompt or pure primitive.**

A surface needs experimenter interaction when **the human is the
source of truth**: the only one who knows what they want (intent
authority — what's your model, what's your axis math, what's your
campaign goal) or the only one who can actuate the fix (environment
authority — start ssh-agent, edit `clusters.yaml`). Everything else
is mechanical and belongs in code.

## The decision table

```
                            │ experimenter (per-experiment intent)  │ deterministic
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
Skill (chat-LLM, inline     │ hpc-build-executor                    │ —
   execution)               │ hpc-classify-axis                     │   ← empty by rule
                            │                                       │
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
Worker prompt (inlined      │ —                                     │ hpc-submit
   into spawn pipeline,     │   ← empty by rule                     │ hpc-status
   delegated execution)     │                                       │ hpc-aggregate
                            │                                       │ hpc-campaign
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
Setup (one-time, imperative │ —                                     │ env probes, SSH agent,
   in /setup-hpc)           │   ← empty by rule                     │ clusters.yaml validation,
                            │                                       │ experiment_roots config
────────────────────────────┼───────────────────────────────────────┼──────────────────────────────
CLI primitive (JSON-in,     │ build-executor, classify-axis,        │ submit-flow, monitor-flow,
   JSON-out, no prompt)     │ interview                             │ aggregate-flow, combine-wave,
                            │                                       │ reconcile, check-preflight, ...
```

Three structural empties confirm the rule:

1. **Top-right empty** — mechanical work does not need an LLM-tolerant
   skill. If you see one there, the prompt is doing what a deterministic
   function should do.
2. **Middle-left empty** — spawned workers cannot actuate human
   authority. That's the escalation contract: workers handle only the
   right column; left-column needs flow back as escalations the user's
   in-chat Claude resolves.
3. **Setup-left empty** — setup is one-time imperative work. Anything
   that asks the user to re-affirm per submit belongs in a skill, not
   setup. Anything you'd ask once-per-machine belongs in setup, not a
   runtime skill.

## How this plays out in the current codebase

* `hpc-build-executor`, `hpc-classify-axis` — left column, inline
  execution. The user's chat agent invokes them via the Skill tool.
  Tolerant prose is fine; the underlying primitive (`build-executor`,
  `classify-axis`) catches LLM mistakes.
* `hpc-submit`, `hpc-status`, `hpc-aggregate`, `hpc-campaign` — right
  column, delegated execution. The text is **inlined** into the
  `claude -p --bare` worker prompt by `spawn_prompt._skill_body`; the
  worker never invokes the Skill tool. These are misnamed today —
  they're really *worker prompts* — and they are eligible for prose
  hardening that real skills are not: snapshot tests on the rendered
  `cacheable_prefix`, token-budget lints, cross-reference lints
  against `SpawnRequest` field names, banned-construct lints. A
  future move to `src/hpc_agent/worker_prompts/` will make this
  explicit.
* **Preflight migrated to setup** — the former `hpc-preflight` skill
  was environment-authority work. Under the rule, environment
  authority belongs in setup (one-time, imperative), not in a runtime
  skill that re-asks per submission. `hpc-agent setup --cluster
  <name>` now does the probe and writes the 24h cache marker
  `/submit-hpc`'s Step 6b gate reads. The skill and its slash command
  are gone.

## When adding a new affordance

Ask, in order:

1. **Is this a one-time-per-machine concern?** If yes → setup. Stop.
2. **Does it need information only the human has, per experiment?** If
   yes → skill (with the underlying primitive doing the actual work
   and catching LLM mistakes). The skill is conversational scaffolding.
3. **Is it mechanical given a JSON spec?** If yes → primitive. If a
   spawned worker should run it as part of a workflow, also add a
   worker prompt that calls the primitive.

If the answer to (2) is "yes, but for any consumer, not just Claude
Code chat," the deliverable is **both** a skill (Claude Code
affordance) and an `--interactive` mode on the underlying CLI so
harnesses without skill discovery still get an equivalent UX.

## What this rule will not tell you

* It does not say a skill must be small or large. `hpc-build-executor`
  is short; a future `hpc-interview` could be long. The constraint is
  consumer, not length.
* It does not say a primitive cannot be backed by a skill. Most are.
  The skill is a *Claude Code adapter* for the primitive — the
  primitive remains the harness-agnostic source of truth.

## See also

* [`adding-a-primitive.md`](adding-a-primitive.md) — the wire-surface
  recipe; complementary to this doc.
* [`sync-checklist.md`](sync-checklist.md) — invariants between
  slash-command surface and CLI.
* `scripts/lint_skill_command_sync.py` — enforces that every workflow
  pair (skill, slash command) has both halves on disk and the slash
  routes to the right skill. The `category` frontmatter field is the
  machine-readable witness for this policy.
