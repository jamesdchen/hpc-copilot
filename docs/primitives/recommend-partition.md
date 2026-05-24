---
name: recommend-partition
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent recommend-partition --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.submit.recommend_partition.recommend_partition
---
## Purpose

Pre-submit advisor: pick a partition for a requested walltime
against the cluster's partition list. Call this before constructing
a submit spec when partition choice isn't obvious — the result's
`recommended_partition` is what you put in the spec's `partition`
field.

Four routing rules: honour an explicit user preference verbatim;
route short jobs to a debug partition for priority leverage (often
10× the backfill probability of `normal`); refuse to route long
jobs to debug (they'd be killed at the cap) and pick the
highest-priority non-debug instead; fall back to the highest-priority
partition if no debug exists.

## Inputs

A `RecommendPartitionSpec` JSON spec with:

- `requested_walltime_sec` (int) — the walltime you intend to ask
  for, in seconds.
- `partitions` (list) — each entry: `{name, is_debug,
  walltime_cap_sec, priority_tier}`. `walltime_cap_sec=null` means
  no cap. Typically read from `clusters.yaml`.
- `user_preferred_partition` (optional str) — if set and present
  in the list, the recommendation honours it verbatim.

## Outputs

A `RecommendPartitionResult` envelope with:

- `recommended_partition` (str) — the chosen partition name, or
  empty when no safe routing exists.
- `rationale` — one of `user_preference_honoured`,
  `debug_short_walltime`, `debug_overrun_refused`,
  `no_debug_partition_available`, `only_debug_available_walltime_too_long`,
  `no_partitions_declared`.
- `message` (str) — a one-line human-readable explanation.
- `leverage_estimate` (float) — `debug.priority_tier /
  fallback.priority_tier` when routing to debug, else `1.0`.

## Errors

None declared. Spec validation errors raise
`pydantic.ValidationError` at the boundary; with a valid spec the
primitive always returns a `RecommendPartitionResult` (a refusal
is encoded as an empty `recommended_partition` + a `rationale`
explaining why).

## Idempotency

Pure function — no SSH, no filesystem, no scheduler query. Calling
twice with the same input yields the same output.

## Usage

Slash commands and agents call this as a standalone advisor before
filling the submit spec's `partition` field. It is intentionally
not composed into `submit-flow` or `plan-throughput`: partition
choice is a routing concern, orthogonal to the throughput concern
of packing tasks into waves.

**Schemas:**
[`recommend_partition.input.json`](../../src/hpc_agent/schemas/recommend_partition.input.json),
[`recommend_partition.output.json`](../../src/hpc_agent/schemas/recommend_partition.output.json).
