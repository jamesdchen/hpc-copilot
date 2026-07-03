---
name: submit-s3
verb: workflow
side_effects:
- scheduler-submit: <cluster> (main array)
- ssh: <cluster> (status poll + wave combine)
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
  cli: hpc-agent submit-s3 --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.submit_blocks.submit_s3
---
## Purpose

Submit block **S3 — submit & watch** (docs/design/human-amplification-blocks.md
§3). The canary was verified and greenlit in [`submit-s2`](submit-s2.md), so S3
launches the main array (Phase-2 of the two-phase gate) via `launch_main_array`,
then runs `monitor-flow` to a terminal/timeout state and arms the next monitor
tick with `decide-monitor-arm`. **Runs unattended — no human boundary inside.**
A clean completion flows on to [`submit-s4`](submit-s4.md); an anomaly
(failed/abandoned) or a timeout is itself the block terminator (§5).

## Inputs

A `SubmitS3Spec` JSON spec with:

- `submit` — the SAME [`SubmitAndVerifySpec`](submit-and-verify.md) S2 used. S3
  launches its main array with canary off and the rsync/deploy/preflight skips
  Phase 1 already paid.
- `canary_run_id`, `canary_job_ids` (optional) — the verified canary's ids from
  S2, threaded onto the result for provenance.
- `monitor` — a [`MonitorFlowSpec`](monitor-flow.md) for the poll loop.
- `invocation_argv` — the exact `/monitor-hpc` argv the next tick re-invokes;
  stamped into the `decide-monitor-arm` cron args.
- `user_invoked_via_loop` (optional) — true iff this tick runs under `/loop`.

## Outputs

A `SubmitBlockResult` (`block="s3"`) with a `brief`:

- `main_run_id`, `main_job_ids`, `total_tasks`, `canary_run_id`.
- `lifecycle_state`, `last_status`, `combined_waves`, `failed_waves`,
  `escalation_reason`, `ticks`, `elapsed_seconds` — from `monitor-flow`.
- `monitor_arm` — the `decide-monitor-arm` decision (arm/cadence/cron args).

`stage_reached` ∈ `watching_terminal` (`needs_decision=false` — proceed to S4) ·
`watching_timeout` / `watching_anomaly` (`needs_decision=true` — §5 terminators).

## Errors

`spec_invalid`, `ssh_unreachable`, `remote_command_failed`, `cluster_unknown`.

## Idempotency

Idempotent on `submit.submit.run_id`. The main-array launch is deduped by
`submit-flow`'s `cmd_sha`; `monitor-flow` is idempotent (re-invoking after a
terminal return is a no-op).

## Usage

```
hpc-agent submit-s3 --spec spec.json --experiment-dir <dir>
```

Fire after the S2 greenlight. On `watching_terminal`, suggest S4. On
`watching_anomaly`, draft a recovery proposal from `escalation_reason` +
`last_status` for the `y`/nudge loop — never silently retry (§3).
