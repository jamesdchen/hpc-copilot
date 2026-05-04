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
  cli: hpc-mapreduce house-edge --profile <name> --cluster <name> [--cmd-sha <sha>]
  python: claude_hpc.agent_cli.cmd_house_edge
exit_codes:
- 0: ok
- 1: spec_invalid
- 3: internal
---

## Purpose

Compare the planner's `--test-only` predictions for Submit→Start latency
against what actually happened on the cluster. Validates that the
backfill probe is finding real windows and surfaces miscalibration when
the lattice is consistently off.

Pure read; no SSH. Reads sidecars produced by `runtime_prior` plus the
`predicted_eta` sidecar that the planner writes during scoring.

## Compose with

- Common predecessors: at least a handful of submits that ran through
  `score-submit-plan` (which seeds `predicted_eta`) and finished long
  enough ago that `started_at` was recorded.
- Common successors: `score-submit-plan` for the next batch — consumers
  can use `calibration_ratio` to decide whether to widen or narrow the
  planner's lattice search.

## Notes

- Samples without a `predicted_eta` (typically open-loop submits that
  bypassed the planner) are dropped; `n_with_prediction` reflects the
  filtered count.
- A `calibration_ratio` near 1.0 with low `p95_delta_sec` means the
  planner is well-calibrated. Big positive `mean_delta_sec` means jobs
  consistently sit longer than predicted — usually a sign the cluster
  metric snapshot was stale.
- Companion primitive `walltime-drift` covers the other half of the
  lattice (walltime cliff vs. utilization). Together they form the
  closed-loop calibration dashboard.
