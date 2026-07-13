---
status: plan
---
# Handoff — greenlit packages swarm (2026-07-12)

Prepared for an external Opus swarm picking up the signed-off work after the
async-refill series landed on `claude/tool-latency-reduction-iqpgcr`. The
authoritative orchestration script is `mcp-latency-docs-packages.workflow.js`
beside this file — run it verbatim (it is a Claude Code Workflow script:
`Workflow({scriptPath: ...})`), or execute its per-unit prompts manually as
eight independent agent tasks. Standing owner directive on model tiers:
**anything design- or plan-related runs on `fable`** (the highest tier);
implementation, integration, and review run on `opus`. The script's
Design-phase architect is pinned accordingly. If the launching account lacks
Fable access, flip design/plan pins to `opus` and note the downgrade in the
run report. It was launched once and stopped early at the
owner's request; **no `pkg/*` staging branch was created**, so every unit
starts fresh.

## What is already DONE (do not rebuild)

- **Async-refill Phase 1 (#362)** — landed by the first swarm as a commit
  series on `claude/tool-latency-reduction-iqpgcr` (see the PR): the
  `campaign-refill` actor (`ops/campaign_refill.py`) driven off
  `campaign-watch`'s new `watching_refill` stage via `infra/block_chain.py`,
  the trial-tokens sidecar fix, scaffold + property tests, docs/rulings
  closure. Phase 2 (live cluster verify, `scripts/campaign_async_live_verify.py`)
  still gates non-experimental status — that needs a real cluster, not a swarm.
- `hpc-agent setup` already registers mcp-serve with
  `--allow-mutations --catalog curated` (`agent_assets.py::_MCP_SERVER_ENTRY`);
  do not re-add.

## What the packages swarm builds (user-signed-off 2026-07-12)

Three packages, eight file-disjoint units (full prompts in the script):

1. **MCP completion** — `m-server` (curated additions for `read-decisions`,
   `verify-relay`, `attention-queue` (+ `poll-detached`); server-side
   rendezvous/skill-return relay in the in-process runner; skill-MCP-reachability
   lint), `m-poll` (new non-blocking `poll-detached` query), `m-elicit`
   (elicitation capability-1 client proof over `tests/_mcp_harness.py`).
2. **Latency** — `l-engine` (SSH engine default-ON under mcp-serve, fail-open,
   env opt-out), `l-snap` (disk-backed ClusterSnapshot cache copying
   `ops/preflight/probe_cache.py` idiom; do NOT touch `cluster_history/`),
   `l-fastpath` (narrow the plugin wholesale fast-path disable in
   `cli/dispatch.py` — verify the guard-can-fire analysis first).
3. **Doc honesty** — `d-rewrite` (`docs/internals/submit-sequence.md` +
   `docs/workflows/code-driven-orchestration.md` rewritten against block-drive
   reality; both still narrate the deleted `claude -p` worker / nonexistent
   resolver modules), `d-pins` (two `tests/contracts` pins: doc-referenced
   console scripts/modules must exist; design-doc `status:` headers sane).

Explicitly NOT signed off: the AVL vendor-neutrality swarm (T1–T8 in
`docs/design/anti-vendor-lockout.md`) — parked, and a `commit-and-advance`
verb — rejected (consent write path stays solely `append-decision`).

## Non-negotiables the units must respect

- Repo doctrine: `docs/internals/engineering-principles.md` (guards must fire,
  determinism boundary, library-knowledge test). Caches are optimisations,
  never correctness gates (fail-open + env bypass).
- Only the integrator runs the regen scripts
  (`scripts/build_schemas.py`, `bake_operations_json.py`,
  `build_operations_index.py`, `build_primitive_index.py`,
  `build_primitive_frontmatter.py`, `build_verb_module_map.py`, then
  `check_no_pending_primitive_docs.py`), then `ruff` + `mypy` +
  `pytest -q -m 'not slow'`, then a structured commit series.
- Build units work in isolated worktrees, one commit each on a `pkg/<key>`
  branch; integrator merges in the script's stated order (m-server last).
- The architect phase's four open design calls (server relay injection point +
  double-fire strategy; plugin fast-path safety; poll-detached shape;
  engine-on degradation) must be settled against the tree before building —
  the script's Design phase prompt enumerates exactly what to read.

## Context for the incoming swarm

Six structured subsystem ingestion maps (kernel/wire, infra, ops pipelines,
state/decision, execution/domain, cli/install-surface, plus a gap-closure map
for meta/scripts/examples/skills) were produced this session; the durable
facts they surfaced are cited inline in the two workflow scripts' prompts.
Key single facts: the SSH engine (`infra/ssh_engine.py`) is default-OFF and
was hardened from mcp-serve incidents; `_fast_dispatch_enabled()` disables the
CLI fast path whenever any plugin is installed; the curated MCP catalog =
verbs whose Result declares `next_block` ∪ `_CURATED_EXTRA_VERBS`
(`_kernel/extension/mcp_server.py`).
