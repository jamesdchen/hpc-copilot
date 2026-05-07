---
name: recommend-partition
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce recommend-partition --spec <path>
  python: claude_hpc.atoms.recommend_partition.recommend_partition
---
# recommend-partition

Route a job to the best partition using a four-rule priority system. SLURM debug partitions often run at much higher priority tier (10× vs 1×) but cap walltime at 1 hour; this primitive honors user preference, routes short jobs to debug for leverage, refuses long jobs to debug (which would be killed), and recommends the best fallback. Each decision includes a `rationale` and `leverage_estimate` to help the agent understand the tradeoff.

## Inputs

- `requested_walltime_sec` (integer) — Job's requested wall-time in seconds.
- `partitions` (list of objects) — The cluster's partition definitions. Each object has:
  - `name` (string) — Partition name.
  - `priority_tier` (integer, default 1) — SLURM PriorityTier for this partition.
  - `walltime_cap_sec` (integer, optional) — Hard cap on walltimes this partition accepts (often 1h for debug).
  - `is_debug` (boolean, default false) — Mark whether this is a debug partition.
- `user_preferred_partition` (string, optional) — User's explicit preference; when set and exists, the primitive honours it verbatim and returns.

## Outputs

A `RecommendPartitionResult` object with:

- `recommended_partition` (string) — Name of the chosen partition.
- `rationale` (string) — One of: `"user_preference_honoured"`, `"debug_short_walltime"`, `"debug_overrun_refused"`, `"default_long_walltime"`, `"no_debug_partition_available"`.
- `message` (string) — Human-readable explanation of the routing decision.
- `leverage_estimate` (float, default 1.0) — Multiplicative speedup (priority-tier ratio) the recommendation buys vs. the default partition. Example: 10.0 means 10× backfill leverage on debug.

## Errors

None declared. Spec validation errors raise `pydantic.ValidationError`; with a valid spec the primitive always returns a `RecommendPartitionResult` (the four routing rules are exhaustive, with rule 4 as the default fallback).

## Idempotency

Pure local routing logic — calling twice with the same inputs produces the same output.

## Notes

- Rule 1 (User Preference): If `user_preferred_partition` is set and exists, use it unconditionally. The message notes that the smart router would have picked differently, giving context.
- Rule 2 (Debug Short): If a debug partition exists and `requested_walltime_sec ≤ walltime_cap_sec`, route to debug for leverage.
- Rule 3 (Debug Overrun): If a debug partition exists and `requested_walltime_sec > walltime_cap_sec`, refuse debug (job would be killed mid-flight) and route to the highest-priority non-debug partition.
- Rule 4 (Fallback): If no debug partition exists, recommend the highest-priority partition.
- The `leverage_estimate` is the ratio `debug.priority_tier / fallback.priority_tier` when recommending debug; otherwise 1.0. This helps the agent understand whether the routing difference is meaningful.
