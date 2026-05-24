---
name: recommend-partition
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.atoms.recommend_partition.recommend_partition
---
# recommend-partition

Route a job to the best partition using a four-rule priority system.
SLURM debug partitions often run at a much higher priority tier (10×
vs 1×) but cap walltime at one hour; this primitive honours user
preference, routes short jobs to debug for leverage, refuses long
jobs to debug (which would be killed), and recommends the best
fallback. Each decision includes a ``rationale`` and
``leverage_estimate`` so the composing primitive can surface the
tradeoff.

## Composers

Called by:

- ``plan-throughput`` — uses the recommendation when packing a
  task grid into batched submission waves.
- ``submit-flow`` — uses it implicitly via ``plan-throughput``.

Not invoked by the agent directly; ``agent_facing=False`` and no
standalone CLI verb.

## Invariants

- Pure local routing logic — no SSH, no filesystem, no scheduler
  query.
- Calling twice with the same inputs produces the same output
  (``idempotency_key`` is intentionally absent; the function is
  stateless).
- The four routing rules are exhaustive; rule 4 is the default
  fallback so a recommendation always comes back.
- ``leverage_estimate`` is the ratio
  ``debug.priority_tier / fallback.priority_tier`` when
  recommending debug; otherwise ``1.0``.

## Coupling

- Input shape: ``RecommendPartitionSpec`` (see
  ``src/hpc_agent/_wire/queries/recommend_partition.py``).
- Output shape: ``RecommendPartitionResult`` with
  ``recommended_partition``, ``rationale`` (one of the four
  enumerated values), ``message``, ``leverage_estimate``.
- Cluster partition definitions flow in via the spec; the primitive
  doesn't read clusters.yaml itself — that's the composer's
  responsibility.

## Failure modes

None declared. Spec validation errors raise
``pydantic.ValidationError`` at the boundary; with a valid spec the
primitive always returns a ``RecommendPartitionResult``.

**Schemas:**
[``recommend_partition.input.json``](../../src/hpc_agent/schemas/recommend_partition.input.json),
[``recommend_partition.output.json``](../../src/hpc_agent/schemas/recommend_partition.output.json).
