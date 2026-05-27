# Internals

Documents that live in this directory are for framework maintainers — design
notes, recipes for adding internals, and architecture deep-dives. They are
**not** the agent surface (see [`docs/primitives/`](../primitives/) and
[`docs/reference/`](../reference/) for that).

## Index

| Doc | Purpose |
|---|---|
| [`adding-a-primitive.md`](adding-a-primitive.md) | Step-by-step recipe for landing a new wire-surface primitive (atom or workflow). |
| [`audit-history.md`](audit-history.md) | Sweep-by-sweep log of the multi-agent repo audit and the bugs each sweep fixed. Historical record of what's been combed and how the severity tail fell. |
| [`campaign-lifecycle.md`](campaign-lifecycle.md) | Design rationale for the campaign / headless shift — why `load-context` + the `delegate` block + `hpc-campaign-driver` replaced the original armed-line Stop hook and the conversation-as-state slash loop. Read before changing the campaign surface. |
| [`parallelization-axes.md`](parallelization-axes.md) | The five-axis model of parallelization (sweep dimensions, scheduling axis, wave structure, stage DAG, DataAxis) — what each is for, how it operates, how they compose at submit time. Disambiguates the multiple "axis" concepts in the framework. |
| [`skill-policy.md`](skill-policy.md) | The three-layer / four-surface model for agent-facing markdown: slashes (interview) → workflow skills (decision) → worker prompts (execution). Skills MUST NOT prompt the human; slashes MAY. Machine-enforced via the `category:` frontmatter field. |
| [`state-model.md`](state-model.md) | What state files exist, what each contains, which primitives read/write them. Per-user state under `~/.claude/hpc/<repo>/`; per-experiment state under `<exp>/.hpc/`. |
| [`submit-sequence.md`](submit-sequence.md) | End-to-end walkthrough from `/submit-hpc` typed in chat to results landing in `aggregated.json`. Traces the slash → skill → bare worker → primitives → cluster pipeline. |
| [`sync-checklist.md`](sync-checklist.md) | Invariants between the slash-command surface and the `hpc-agent` CLI — what must stay aligned when either changes. |

## When to add a doc here

- Architecture or algorithm design that a maintainer would need to understand
  before changing the implementation.
- Recipes / playbooks for repeated maintenance tasks.
- Cross-cutting invariants that span multiple subpackages.

If the doc is for a primitive caller, it belongs in `docs/primitives/` (one
file per primitive) or `docs/reference/` (cross-cutting wire contracts).
