---
name: plan-throughput
verb: query
inputs:
- name: cluster
  type: string
  description: Cluster name; its `constraints:` block in clusters.yaml supplies the
    scheduler limits.
- name: total_tasks
  type: integer
  description: Total task count to pack into waves (the grid cardinality).
- name: est_task_duration_s
  type: integer
  description: Optional estimated per-task wall seconds. Enables the walltime-feasibility
    check and total-time estimate.
  default: null
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent plan-throughput --cluster <name> --total-tasks <n> [--est-task-duration-s
    <n>]
  python: hpc_agent.atoms.plan_throughput.plan_throughput
exit_codes:
- 0: ok
- 1: spec_invalid / cluster_unknown
---

## Purpose

Pack a task grid into batched submission waves. Given a cluster's scheduler constraints (`max_array_size` / `max_walltime` / `max_concurrent_jobs` / `est_spin_up`) and a total task count, it computes how many scheduler arrays the grid splits into, how those arrays group into concurrency-bounded waves, and the per-wave task-id `wave_map` the cluster-side combiner consumes.

This is the deterministic core that `/submit-hpc` Step 4b used to do as inline library calls (`compute_submission_plan` + `build_wave_map`). Both are pure functions over `(constraints, total_tasks)`, so they belong behind a primitive any caller — the interactive skill or a headless integrator — invokes, rather than a block of Python embedded in skill prose. Pure-local: it reads `clusters.yaml` and computes; no SSH.

## Compose with

- **Predecessors**: grid expansion (produces `total_tasks`); `clusters-describe` to confirm the cluster name.
- **Successors**: `build-submit-spec` / `submit-spec` — thread the returned `wave_map` into the per-run sidecar (`write_run_sidecar(..., wave_map=...)`) so the cluster-side combiner knows which tasks to aggregate after each wave.

## Notes

- **Constraint fallback.** A cluster with no `constraints:` block in `clusters.yaml` falls back to `ClusterConstraints` defaults (`max_array_size=1000`, `max_concurrent_jobs=10`, `max_walltime=24:00:00`). For a grid under the default array size this is effectively a single-array plan.
- **`est_task_duration_s` is optional.** Without it the plan is structural only (batch/wave counts, `wave_map`); `est_total_wall_s` is `null`. With it, the primitive additionally checks a single task fits inside `max_walltime` — raising `spec_invalid` if not — and estimates total wall-clock.
- **`wave_map` keys are strings.** JSON object keys must be strings, so the wave numbers are stringified in the envelope. They are already stringified in the per-run sidecar the combiner reads, so no conversion is needed downstream.
- **Result shape.** `{strategy, total_tasks, total_batches, max_concurrent, n_waves, est_total_wall_s, wave_map, batches}` — `strategy` is a human-readable one-liner; `batches` lists each array's `task_range` / `array_size` / `wave`.
