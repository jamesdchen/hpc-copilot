---
name: submit-s2
verb: workflow
side_effects:
- scheduler-submit: <cluster> (canary only)
- ssh: <cluster> (canary poll + log scan)
idempotent: true
idempotency_key: submit.submit.run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent submit-s2 --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.submit_blocks.submit_s2
---
## Purpose

Submit block **S2 — stage & canary** (docs/design/human-amplification-blocks.md
§3). Runs `submit-and-verify` with `stop_after_canary=True` so the 1-task canary
is submitted and verified but the **main array does NOT launch**, then attaches a
pre-dispatch core-hours estimate. Ends at the "canary green, est. N core-hours"
brief for the `y`/nudge loop; on greenlight, [`submit-s3`](submit-s3.md) launches
the main array.

## Inputs

A `SubmitS2Spec` JSON spec with:

- `submit` — a nested [`SubmitAndVerifySpec`](submit-and-verify.md). Must have
  `submit.canary=True` (S2 gates on a verified canary). The cost estimate is
  computed from `submit.submit` (`total_tasks × resources.walltime_sec ×
  resources.cpus`), so those fields drive the footprint.

## Outputs

A `SubmitBlockResult` (`block="s2"`, `needs_decision=true`) with a `brief`:

- `canary_run_id`, `canary_job_ids`, `verified`, `failure_kind`, `deduped` —
  pass-through from `submit-and-verify`.
- `est_core_hours`, `est_gpu_hours` — the `estimate-core-hours` footprint.
- `cost_estimate` — `{total_tasks, walltime_s, cores_per_task, gpus_per_task,
  est_core_hours, est_gpu_hours}`.
- `verify_result` — the full verify-canary envelope when one ran.

`stage_reached` ∈ `canary_verified` (green — greenlight to S3) · `canary_failed`
(an anomaly terminator; propose a fix) · `deduped` (the run already exists).

## Errors

Inherits from `submit-and-verify`: `spec_invalid`, `ssh_unreachable`,
`remote_command_failed`, `cluster_unknown`.

## Idempotency

Idempotent on `submit.submit.run_id`. A replay is deduped by the underlying
`submit-flow`; S2 surfaces it as `stage_reached="deduped"`.

## Usage

```
hpc-agent submit-s2 --spec spec.json --experiment-dir <dir>
```

Present "canary green, est. N core-hours"; the human answers `y` (proceed to S3)
or a nudge (e.g. "halve the grid"). On `canary_failed`, surface the
`verify_result.stderr_tail` and propose a fix before the main array ever runs.
