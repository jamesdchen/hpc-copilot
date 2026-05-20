---
name: house-edge
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
- name: experiment_dir
  type: path
  default: cwd
outputs:
- name: n_with_prediction
  type: integer
  description: How many recent samples carried both a `predicted_eta` and an observed
    `started_at` for comparison.
- name: mean_delta_sec
  type: number
  description: Mean of (observed_start_seconds − predicted_start_seconds) across samples.
- name: median_delta_sec
  type: number
- name: p95_delta_sec
  type: number
- name: calibration_ratio
  type: number
  description: Ratio of observed to predicted Submit→Start latency. >1 means the planner
    was optimistic; <1 means jobs landed sooner than predicted.
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent house-edge --profile <name> --cluster <name> [--cmd-sha <sha>]
  python: hpc_agent.atoms.house_edge.house_edge
exit_codes:
- 0: ok
- 1: spec_invalid
- 3: internal
---
# house-edge

> **Internal primitive.** Calibration-loop helper consumed by
> debug tooling and `score-submit-plan`'s lattice-tuning logic.
> Not on any agent's hot path.

Compare the planner's `--test-only` Submit→Start predictions
against observed cluster reality. Validates that the backfill
probe is finding real windows; surfaces miscalibration when the
lattice is consistently optimistic / pessimistic.

## Composers

- Operator-driven calibration dashboards
  (`hpc-agent house-edge --profile <p> --cluster <c>`).
- `score-submit-plan`'s lattice-width tuner can read the
  `calibration_ratio` to decide whether to widen or narrow its
  search.

No registered Python `composes=` references.

## Invariants

- **Pure read.** Reads `runtime_prior` samples + the planner's
  `predicted_eta` sidecar. No SSH, no journal mutation.
- **Successful samples only.** `read_samples(...,
  only_successful=True)` — calibration over runs that finished,
  not failed/cancelled ones.
- **Samples without `predicted_eta` are dropped silently.**
  `n_with_prediction` reflects the filtered count, NOT the total
  sample count.

## Coupling

- The five output stats (`n_with_prediction`, `mean_delta_sec`,
  `median_delta_sec`, `p95_delta_sec`, `calibration_ratio`) are
  the public contract. Adding a stat is a wire-extending change;
  removing one breaks the calibration dashboard.
- Pairs with `walltime-drift` (the other half of the calibration
  loop). Refactors that touch `runtime_prior` shape need to
  sanity-check both atoms.

## Failure modes

- Cold start (zero samples with `predicted_eta`) →
  `calibration_ratio=1.0`, `n_with_prediction=0`. Caller must
  not divide by `n_with_prediction` without guarding.
- Stale cluster snapshots produce systematic positive
  `mean_delta_sec` (jobs sit longer than predicted). A
  consistently-positive number across a long sample window is a
  signal that the planner's `inspect-cluster` cache TTL is too
  long.
