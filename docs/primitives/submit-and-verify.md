---
name: submit-and-verify
verb: workflow
side_effects:
- scheduler-submit: <cluster>
- ssh: <cluster> (canary poll + log scan)
idempotent: true
idempotency_key: submit.run_id
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
  cli: hpc-agent submit-and-verify --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.submit_and_verify.submit_and_verify
---
## Purpose

Submit a run plus its 1-task canary, then wait for the canary to
land terminal before returning. One call replaces the two-step flow
of `/submit-hpc` then `/verify-canary`. Useful when the caller wants
to branch once on a `verified` boolean rather than orchestrate the
two halves themselves.

Workflow-composes-workflow: chains `submit-flow` and `verify-canary`
under one envelope. The chain is finite and blocking (no monitor-
polling shape), so the composition is honest.

## Inputs

A `SubmitAndVerifySpec` JSON spec with:

- `submit` — a nested [`SubmitFlowSpec`](submit-flow.md) carrying the
  full submit-side knob set (cluster, run_id, job_env, canary flag,
  etc.). The spec is forwarded verbatim to `submit-flow`. Set
  `submit.canary=False` to skip verification entirely (the workflow
  degenerates to a bare submit-flow call and returns
  `verified=False`).
- `expect_output` (optional) — path the canary should have written;
  forwarded to `verify-canary`.
- `fingerprint` (optional) — relative path under the canary's
  result_dir to SHA256 for drift detection.
- `poll_interval_sec`, `wait_budget_sec` — adaptive poll knobs for
  the canary wait.
- `log_dir`, `file_glob` — cluster-side stderr-scan inputs.

## Outputs

A `SubmitAndVerifyResult` with:

- `run_id`, `job_ids`, `total_tasks`, `deduped` — pass-through from
  the submit half.
- `canary_run_id`, `canary_job_ids` — pass-through from the submit
  half; None when canary was skipped.
- `verified` (bool) — True iff `verify-canary` returned `ok=True`.
  False on any canary-side failure AND when canary verification was
  skipped (no canary fired).
- `failure_kind` — pass-through from `verify-canary`; None on
  success, None when canary was skipped.
- `verify_result` — full `verify-canary` envelope when verification
  ran; None when canary was skipped or submit was deduped.

## Errors

Inherits from both halves:

- `spec_invalid` — malformed spec or invalid canary run id.
- `ssh_unreachable` — pre-flight probe failed or the canary poll
  budget elapsed in failed SSH calls.
- `remote_command_failed` — rsync/deploy/qsub returned non-zero.
- `cluster_unknown` — `submit.cluster` is not in `clusters.yaml`.

## Idempotency

Idempotent on `submit.run_id`. A replay returns the submit half as
`deduped=True` without re-submitting; in that case the workflow
skips the canary verify (no fresh canary to wait on) and returns
with `verified=False`, `verify_result=None`. This matches the
semantics of running the two slash commands manually on a replay.

## Usage

```
hpc-agent submit-and-verify --spec spec.json --experiment-dir <dir>
```

Branch on `data.verified`: True → main array is healthy, monitor as
usual; False → inspect `data.verify_result.stderr_tail` for the raw
canary error before resubmitting.

**Schemas:**
[`submit_and_verify.input.json`](../../src/hpc_agent/schemas/submit_and_verify.input.json),
[`submit_and_verify.output.json`](../../src/hpc_agent/schemas/submit_and_verify.output.json).
