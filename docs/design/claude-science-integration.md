# Claude Science integration — the producer seam (2026-07-29)

Status: **BUILT — producer seam shipped 2026-07-29.** Maintainer chose the
**producer** capability boundary and **both** delivery mechanisms (MCP
connector + packaged skill) on 2026-07-29; the `science` catalog, the
boundary-free skill, and the disjointness contract test landed the same day.
The connector Claude Science registers is
`hpc-agent mcp-serve --catalog science --allow-mutations` (queue-run is a
`mutate`, so mutations are on; only the three producer verbs are reachable, so
enabling them crosses no gate). The disjointness — no `queue-dispatch`, no
`submit-*`, no `append-decision`, no `block-drive` — is pinned by
`tests/test_mcp_science.py` in both mutation-flag states.

## The fit

Claude Science is a generalist coordinating agent that runs on HPC login
nodes and drives clusters over SSH, using curated skills and connectors
(`https://www.anthropic.com/news/claude-science-ai-workbench`). hpc-copilot
is the HPC-job specialist that lives on that same login node and already
ships the two surfaces Claude Science consumes: **skills**
(`slash_commands/skills/*/SKILL.md`) and a **curated MCP server**
(`_kernel/extension/mcp_server.py`, `hpc-agent mcp-serve --catalog curated`).
Integration is pointing Claude Science at a seam that already exists, not new
plumbing.

## The boundary — producer, not driver

This fork's identity is "decision points presented to humans, not routed to
an unreliable agent." Claude Science IS an autonomous agent, so the
integration MUST NOT hand it a gate. The run queue makes this clean: Claude
Science plays the SAME role `campaign-refill` plays — a queue PRODUCER.

- **Claude Science may:** enqueue experiments (`queue-run`, ungated —
  enqueueing spends nothing, §1), read the queue and the fleet
  (`queue-status`), and read the placement authority's reasoning
  (`queue-advance`, pure).
- **Claude Science may NOT:** dispatch (`queue-dispatch`), or touch any
  gated verb. The cluster-boundary `y` still surfaces to the human, exactly
  as when a human enqueues. The morning brief's queue section (built
  2026-07-29) reports back what Claude Science queued and what it is waiting
  on.

The net loop: Claude Science says "run this sweep on GPU" → it lands on the
waiting list → the human approves at dispatch → results flow back through
the normal lifecycle and the morning brief.

## Mechanism — both, with the boundary in exactly one place

- **MCP connector (enforcement).** A new `--catalog science` (a producer
  subset) advertises `queue-run` + `queue-status` + `queue-advance` and
  NOTHING that crosses a gate — in particular NOT `queue-dispatch`, not
  `submit-*`, not `append-decision`, not `block-drive`. The catalog IS the
  boundary; it cannot be bypassed by a skill instruction. Registered in
  Claude Science as a connector: `hpc-agent mcp-serve --catalog science`.
- **Packaged skill (discoverability).** A skill under
  `slash_commands/skills/` teaches Claude Science WHEN and HOW to enqueue
  (resource asks, cluster pins, campaign bases) and to read status back — but
  the skill holds NO boundary logic. If the skill and the catalog ever
  disagree, the catalog wins by construction (an MCP-unreachable verb a skill
  names is simply uncallable — the run-#8 lesson).

## Why a new catalog rather than extending `curated`

`curated` is derived from the block-chain verbs (those with a `next_block`)
plus `_CURATED_EXTRA_VERBS` (`mcp_server.py`), and it deliberately includes
gate-crossing verbs (`block-drive`, `append-decision`) because its consumer
is the human-amplification agent that DOES take the human's y. Claude Science
is a different trust tier — it must not see those. So `science` is a distinct,
narrower catalog, not an edit to `curated`. The two catalogs answer two
different "who is asking" questions.

## Build scope

1. `mcp_server.py`: a `science` catalog = `{queue-run, queue-status,
   queue-advance}` (producer subset), asserted DISJOINT from every gated verb
   by a contract test (the negative is the point: `queue-dispatch` and each
   gated verb must NOT be reachable under `--catalog science`).
2. `cli/mcp.py`: accept `--catalog science`.
3. A skill teaching the enqueue/observe loop, boundary-free.
4. Tests: catalog membership (positive + the disjointness negative), the
   skill's MCP-reachability lint (every verb the SKILL names MCP-direct must
   be reachable under the `science` catalog — the existing
   `lint_skill_mcp_reachability` discipline).
5. Docs + changelog; regen; full gates.

## Open, deferred

- Whether Claude Science should get a standing-consent path (the "driver"
  boundary the maintainer declined for v1) is a future decision, not this
  build. If ever taken, it rides the existing overnight-consent machinery
  with Claude Science as a named actor — never a new gate.
- Modal / on-demand GPU (a Claude Science compute backend) vs. hpc-copilot's
  SSH cluster model: out of scope; hpc-copilot owns the SSH-cluster path, and
  the queue is per-experiment-repo regardless of which agent produced the item.
