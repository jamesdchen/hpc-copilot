---
name: recall
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent recall [--limit <limit>] [--include-runtime] [--include-generator-stats]
    [--root <root>] [--task-kind <task_kind>] [--operator <operator>] [--since <since>]
  python: hpc_agent.ops.memory.recall.recall_campaigns
---
# recall

Walk one or more directory trees for `interview.json` files and
return per-campaign summaries plus pre-computed cross-campaign
aggregations. Drives "last time you ran this kind of campaign…"
context for a fresh interview agent.

## Inputs

The full input schema is at `hpc_agent/schemas/recall.input.json`
(Pydantic-emitted from `_wire/queries/recall.py:RecallSpec`). All
fields optional:

- `root` (str) — filesystem directory to walk recursively. When
  omitted, falls back to
  `~/.hpc-agent/config.json:experiment_roots`. Both empty raises
  `spec_invalid`.
- `task_kind` (str) — exact-match filter against
  `intent.task_kind`. The values are whatever the caller wrote at
  interview time; hpc-agent does not maintain a taxonomy.
  Strings like `"ml-hparam-sweep"` or `"rl-rollout"` are common
  examples but not a canonical set.
- `operator` (str) — exact-match filter against
  `intent.produced_by.operator` (for human-driven campaigns).
- `since` (ISO-8601) — only return campaigns whose
  `_materialized.at` is at or after this timestamp.
- `limit` (int, default 20) — cap on results returned. The total
  match count (pre-truncation) is reported via
  `data.total_matching` so the caller can detect "200 matching
  campaigns; narrow the filter."
- `include_runtime` (bool, default false) — Tier 2 rollup. Walks
  each matched campaign's `.hpc/runtimes/*.json` and aggregates
  `elapsed_sec` + failure rate.
- `include_generator_stats` (bool, default false) — Tier 3
  rollup. Buckets matched campaigns by `task_generator.kind` and
  reports observed parameter envelopes.

## Outputs

`{ok: true, data: {campaigns, total_matching, showing, rollup}}`.
Each `campaigns[i]` projects the prior-decision fields the next
interviewer would compare against (`goal`, `task_kind`,
`task_count`, `budget`, `abort_if`, `cluster_target`,
`task_generator`) — not just file-listing metadata. The `rollup`
block always carries Tier 1 aggregations (count, distributions);
`runtime_rollup` and `generator_rollup` appear only when the
respective opt-in flags are set.

## Errors

- `spec_invalid` — `root` is empty and
  `~/.hpc-agent/config.json:experiment_roots` is also empty.

## Idempotency

Pure read; no side effects. Safe to invoke arbitrarily.

## Notes

`recall` is the canonical entry-point for cross-campaign memory.
The interview agent calls it before drafting a new
`interview.json` so it can ask "operator's prior runs in this
family targeted cluster X with LR range Y; reuse?" — turning a
cold start into a warm one.

Tier 2 and Tier 3 rollups are opt-in because they walk additional
files (per-task runtime ledgers and per-recipe params); the
default Tier-1 path is cheap (one read per `interview.json`).

### When to use this vs your caller's own memory model

`recall` is scoped to *interview-time grounding*: surfacing what
prior campaigns of the same task family already explored, so the
next interview's range / budget / abort decisions start informed.
It's deliberately a thin filesystem index — recency-sorted matches
plus pre-computed aggregations — not a metric store.

Use `recall` when the calling agent wants:

- A pre-interview lookup that's identical under Claude Code, cron,
  or any external orchestrator (filesystem is the index; no
  long-lived process or external DB).
- Observed parameter envelopes across past campaigns of one
  `task_kind`, without the calling agent re-reading every
  `interview.json` itself.
- Walltime and failure-rate baselines (Tier 2) for resource
  sizing, sourced from the same per-task runtime ledgers
  `runtime-prior` reads.

Stick with the caller's own memory model when:

- The calling agent already maintains a richer experiment-level
  store (research-paper-scale provenance, vector embeddings,
  cross-agent project state). `recall` does not replace it; it
  complements it at the interview seam.
- You need to *reason* over the ranges, not just retrieve them.
  `recall` reports observed envelopes only; recommending whether
  to tighten, widen, or pivot is the calling agent's call.
- You need per-task `metrics.json` aggregated across campaigns.
  `recall` deliberately doesn't fold metric content because every
  experiment emits different keys; metric inspection stays a
  per-campaign concern.

The two layers coexist: an integrator's experiment-level journal
keys on `experiment_id`; hpc-agent's interview / recall surface
keys on `campaign_dir`. Different scopes, no overlap.
