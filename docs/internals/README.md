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
| [`skill-policy.md`](skill-policy.md) | The forcing rule for where new LLM-facing markdown lives, framed around the two consumers of hpc-agent: humans (via slash commands, which elicit intent) and other agents (via skills, which decide autonomously). Skills MUST NOT prompt the human; slashes MAY. Machine-enforced via the `category:` frontmatter field. |
| [`sync-checklist.md`](sync-checklist.md) | Invariants between the slash-command surface and the `hpc-agent` CLI — what must stay aligned when either changes. |

## When to add a doc here

- Architecture or algorithm design that a maintainer would need to understand
  before changing the implementation.
- Recipes / playbooks for repeated maintenance tasks.
- Cross-cutting invariants that span multiple subpackages.

If the doc is for a primitive caller, it belongs in `docs/primitives/` (one
file per primitive) or `docs/reference/` (cross-cutting wire contracts).
