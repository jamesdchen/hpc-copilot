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
  cli: hpc-mapreduce walltime-drift --profile <name> --cluster <name> [--cmd-sha <sha>]
    [--base-safety-mult <f>]
  python: hpc_mapreduce.agent_cli.cmd_walltime_drift
exit_codes:
- 0: ok
- 1: spec_invalid
- 3: internal
---

## Purpose

Closed-loop calibration query. Inspects past samples written by
`runtime_prior.append_sample` and estimates how often the planner's
walltime guess hit the cliff (job killed by scheduler) versus how often
it sat well below the wall. Returns a recommended `safety_mult`
adjustment plus a short rationale.

Pure read; no SSH, no mutations. The caller decides whether to apply the
recommendation when calling `score-submit-plan`.

## Compose with

- Common predecessors: enough completed runs that
  `read-runtime-prior` returns `needs_canary: false`.
- Common successors: `score-submit-plan` (pass `adjusted_safety_mult`
  to the planner so the next batch is calibrated).

## Notes

- A small `n_recent` (e.g. <5) means the rationale will say "insufficient
  signal" and `adjusted_safety_mult` will equal `base_safety_mult`.
- Filtering by `cmd_sha` is recommended whenever `.hpc/tasks.py` changes
  meaningfully; otherwise samples from a heavier prior workload would
  drag the estimate.
- Companion primitive `house-edge` validates the planner's start-time
  predictions; together the two give a calibration dashboard for the
  smart-submit lattice.
