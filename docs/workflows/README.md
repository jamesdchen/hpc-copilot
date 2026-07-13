# Workflows

User-facing guides for the long-running pipelines an operator drives —
how to think about them, what state they persist, how iterations
compose. Read these to use the framework; read
[`docs/internals/`](../internals/) to change it.

## Index

| Doc | Purpose | Pairs with |
|---|---|---|
| [`campaign.md`](campaign.md) | The campaign loop — closed-loop iteration over a tasks.py, scaffolded by `/campaign-hpc`, driven by `hpc-campaign-driver`. | [internals/campaign-lifecycle](../internals/campaign-lifecycle.md) |
| [`memory-across-campaigns.md`](memory-across-campaigns.md) | The `interview` ↔ `recall` loop — how each campaign's intent persists into structured artifacts that ground the next campaign's interview. | [primitives/recall](../primitives/recall.md) · [primitives/interview](../primitives/interview.md) |
| [`migration-from-hpc-yaml.md`](migration-from-hpc-yaml.md) | One-page upgrade guide for users coming from the pre-primitive `.hpc.yaml` config style. | [reference/cli-spec](../reference/cli-spec.md) |
| [`code-driven-orchestration.md`](code-driven-orchestration.md) | The third consumption style — a plain program owns the loop (`drive_once`), LLM calls only at typed judgement points (`LlmJudgementResolver` / `structured()` / escalation-as-data). | [integrations/CONTRACT](../integrations/CONTRACT.md) · [design/dag-kernel](../design/dag-kernel.md) |

## How these docs relate to skills and architecture

The triangle:

```
                ┌──────────────────────────────┐
                │  docs/architecture.md        │
                │  Layering rules; where each  │
                │  package lives in the DAG.   │
                └─────────────┬────────────────┘
                              │
            ┌─────────────────┴─────────────────┐
            │                                   │
            ▼                                   ▼
  ┌──────────────────────┐         ┌────────────────────────────────┐
  │  docs/workflows/     │ ◄─────► │  src/hpc_agent/slash_commands/ │
  │  (this directory)    │  pair   │  skills/<name>/SKILL.md        │
  │  "what to expect     │  with   │  "what to do, step by          │
  │   running it"        │         │   step"                        │
  └──────────────────────┘         └────────────────────────────────┘
```

- **`docs/architecture.md`** is the layering map — atoms, flows,
  planning, infra, state. Read it to understand WHERE code lives.
- **`docs/workflows/*.md`** (this directory) is the operator's mental
  model — what one campaign / one interview-recall loop *does* over
  time, what artifacts persist, what state the next iteration sees.
- **`src/hpc_agent/slash_commands/skills/<name>/SKILL.md`** is the step-by-step
  procedure an agent follows — autonomous decision logic callable by
  the user's chat (via the `Skill` tool) or by any other agent harness
  (MARs, notebooks, cron drivers). The skill is the *recipe*; the
  workflow doc is the *story*. Human elicitation, when needed, lives
  in the paired slash command (`src/hpc_agent/slash_commands/commands/<stem>.md`),
  not in the skill — see `docs/internals/skill-policy.md`.
- **`docs/internals/`** is the maintainer's view — design rationale,
  rejected alternatives, when to change the surface.
