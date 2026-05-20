---
name: score-submit-plan
verb: query
inputs:
- name: profile
  type: string
- name: cluster
  type: string
- name: candidates
  type: list[string]
  description: Constraint expressions (comma-separated; pipe inside a single candidate).
    Defaults to one per gpu_type plus their union.
  default: null
- name: cmd_sha
  type: string
  description: Filter applied to read-runtime-prior.
  default: null
- name: experiment_dir
  type: path
  default: cwd
side_effects:
- ssh: <cluster> (delegates to inspect-cluster)
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent plan-submit --profile <name> --cluster <name> [...]
  python: hpc_agent.planning.planner.plan_submit
exit_codes:
- 0: ok
- 1: spec_invalid / cluster_unknown
- 2: ssh_unreachable
- 3: internal
---

## Purpose

Combine `inspect-cluster` + `read-runtime-prior` into a candidate-constraint scorecard. The slash command hands this JSON to Claude (or the user) for cost-model judgment over which constraint to submit under.

## Compose with

- Common predecessors: `clusters-describe` (to confirm the cluster), `read-runtime-prior` (this primitive calls it internally; explicit predecessor only when the caller wants the priors separately).
- Common successors: `submit-spec` (with the chosen constraint baked into the spec).

## Scoring rubric

When `needs_canary=false`, score each `candidates[i]` and pick the one with smallest `total_etc`:

```
p95(c)        = max(quantiles[gpu]['p95'] for gpu in c.runtime_prior_quantiles_sec)
p_fail(c)     = max(c.p_fail_30d.values(), default=0.0)
total_etc(c)  = eta_sec_via_test_only(c) + p95(c) + p_fail(c) * (eta_sec(c) + p95(c))
```

`eta_sec_via_test_only` is the queue-wait estimate from a test-only `srun`/`qsub`. The `p_fail` term turns into expected wasted-wall-clock if the run dies — captures the cost of landing on a flaky node even when the queue is short. Tie-break: prefer the narrower constraint (smaller `pool_size`) — same expected cost, less risk of getting an outlier-slow GPU.

Suggested walltime: `chosen.p95(c) * safety_margin` (default `1.3`). Covers the worst GPU type the constraint admits without ballooning the budget.

**Empty-quantiles edge case**: a candidate's `runtime_prior_quantiles_sec` may be empty even when the rollup is non-empty (e.g. priors exist for `a100` but the candidate constraint is `v100`). Skip such candidates from scoring; if they're the *only* candidates available, the caller should drop back to a canary submission for that constraint.

**Empty-ETA edge case**: when `eta_sec_via_test_only` is `null` (sbatch `--test-only` failed or scheduler is SGE), substitute the cluster's typical queue depth or just `0` and continue — the runtime prior dominates the cost most of the time.

## Adversarial backfill mode

Default-on when priors exist. On top of the constraint scorecard the primitive returns resource-shrink recommendations and a probed launch tuple:

1. **Walltime shrink** — `p95 × 1.30` from `runtime_prior.elapsed_sec` (≥5 samples per GPU type).
2. **Footprint shrink** — `--mem` from `peak_host_mem_mb` (`p95 × 1.50`, ≥10 samples) and `--cpus-per-task` from `cpu_seconds_used / elapsed_sec`. Both axes only **shrink below** the caller's defaults — never grow — to avoid silent OOM / cliff kills.
3. **Probe lattice** — sweep `(walltime × mem × constraint)` via `sbatch --test-only` and pick the variant SLURM predicts will start earliest. The winner is surfaced as `recommended_tuple` with `predicted_eta_sec` set when a fitting backfill window was confirmed.

`array_reshape.recommended_max_array_size` is a cluster-wide reshape the caller can apply unconditionally; `walltime_split` requires `requires_checkpointing` confirmation before chaining.

**Closed-loop calibration.** The planner reads recent runtime samples and tunes the walltime safety multiplier; the top-level `walltime_drift` field reports `{base_safety_mult, adjusted_safety_mult, rationale}`. Pair it with `hpc_agent.forecast.calibration.record_prediction_sidecar` post-submit so completion ingestion can validate the calibration.

## Notes

- When `needs_canary=true`, `canary_plan` carries a 1-task probe spec — the caller submits the canary, ingests its result via `runtime_prior.append_sample`, then re-invokes this primitive to score normally.
- `stressed_nodes` are advisory soft-excludes — the caller decides per-node whether to actually exclude based on co-tenant context.
