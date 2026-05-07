---
name: walltime-drift
verb: query
inputs:
- name: profile
  type: string
- name: cluster
  type: string
- name: cmd_sha
  type: string
  description: Filter samples to one cmd_sha (recommended after .hpc/tasks.py edits).
  default: null
- name: base_safety_mult
  type: float
  default: 1.3
  description: Starting safety multiplier the planner currently uses; the recommendation
    is computed relative to this value.
- name: experiment_dir
  type: path
  default: cwd
outputs:
- name: n_recent
  type: integer
  description: How many recent samples informed the drift estimate.
- name: n_cliff_events
  type: integer
  description: Count of past samples that hit the walltime cliff (kill-by-scheduler).
- name: n_near_misses
  type: integer
  description: Count of past samples that finished above a configurable utilization
    threshold of the wall they were granted.
- name: weighted_cliff_rate
  type: number
- name: median_utilization
  type: number
- name: base_safety_mult
  type: number
- name: adjusted_safety_mult
  type: number
  description: Recommended safety_mult for the next plan-submit. May equal base when
    no signal.
- name: rationale
  type: string
  description: Human-readable one-liner explaining why the recommendation moved (or
    didn't).
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent walltime-drift --profile <name> --cluster <name> [--cmd-sha <sha>]
    [--base-safety-mult <f>]
  python: claude_hpc.atoms.walltime_drift.walltime_drift
exit_codes:
- 0: ok
- 1: spec_invalid
- 3: internal
---
# walltime-drift

> **Internal primitive.** Closed-loop calibration query consumed
> by `score-submit-plan`'s safety-multiplier tuner.

Estimate how often the planner's walltime guess hit the cliff
(job killed by scheduler) vs sat well below the wall. Returns a
recommended `safety_mult` adjustment plus a short rationale. The
caller decides whether to apply the recommendation.

## Composers

- `score-submit-plan`'s safety-mult tuner: feeds
  `adjusted_safety_mult` into the next plan-submit so the
  walltime ask is calibrated against observed cliff rate.
- Calibration dashboards alongside `house-edge`.

## Invariants

- **Pure read.** Reads `runtime_prior` samples (both successful
  and failed — `read_samples(only_successful=False)`). No SSH,
  no journal mutation.
- **Insufficient-signal contract.** When `n_recent < 5`,
  `adjusted_safety_mult` equals `base_safety_mult` and `rationale`
  says "insufficient signal." Callers can trust that the
  recommendation only moves when there's actual data.
- **`base_safety_mult` is required.** No default — the caller
  passes the value they're currently using so the relative
  recommendation makes sense.

## Coupling

- The walltime-cliff threshold (sample's `elapsed_sec >= 0.95 *
  granted_walltime_sec` is hardcoded in
  `forecast.calibration.compute_walltime_drift`. Tightening or
  loosening that threshold is a behavior-change.
- Pairs with `house-edge` (Submit→Start latency calibration). A
  refactor that touches `runtime_prior` shape needs to validate
  both atoms still produce sensible signals.

## Failure modes

- Edited `.hpc/tasks.py` → samples from prior workload drag the
  estimate. Caller should pass `cmd_sha=<new>` to filter; the
  framework doesn't enforce this.
- Cluster with no walltime kill (job runs to completion even
  past the wall — rare but seen on permissive partitions) →
  `n_cliff_events=0`, recommendation stays at base.
