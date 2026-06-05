---
name: status-pipeline
verb: workflow
side_effects:
- ssh: <cluster> (status polls)
- writes-tick-log: <experiment_dir>/<run_id>.monitor.jsonl
idempotent: true
idempotency_key: status.monitor.run_id
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
  cli: hpc-agent status-pipeline --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.status_pipeline.status_pipeline
---
# status-pipeline

The deterministic status **spine** as one call. Folds
`worker_prompts/status.md` Steps 2-4 — pick the wait-until-terminal surface,
run it to terminal/budget, and branch on `lifecycle_state` — into a single
workflow primitive that runs the branch logic in code and returns one typed
outcome.

## Why this exists

The wait-then-dispatch is mechanical: run [monitor-flow](monitor-flow.md), then
map its `lifecycle_state` to the next move (`complete` → aggregate; `timeout` →
re-watch; `failed`/`abandoned` → decide). That is control flow the agent was
hand-walking. `status-pipeline` runs it, so the agent's role shrinks from "run
the monitor and branch on the lifecycle" to "call one verb and read
`stage_reached`". It is the [submit-pipeline](submit-pipeline.md) pattern
applied to the status workflow.

## Composition

```
monitor-flow  →  (lifecycle_state dispatch)
```

`monitor-flow` is an `ops`-subject verb, so the composite needs no
cross-subject import. Scope is the **blocking / wait-until-terminal** surface
(the canonical campaign-loop case the driver sets via `blocking: true`). The
one-shot [poll-run-status](poll-run-status.md) snapshot stays a direct verb —
it has no branch to fold; the caller decides its own cadence.

## Inputs / outputs

See `hpc_agent/schemas/status_pipeline.{input,output}.json`. The input embeds a
full `MonitorFlowSpec` under `monitor` (`run_id` + poll cadence + wall-clock
budget).

The output carries a single `stage_reached` ∈ `{complete, timeout, failed,
abandoned}` and a `needs_decision` flag, plus the monitor's `last_status`,
`combined_waves`, `failed_waves`, `ticks`, `elapsed_seconds`, and
`escalation_reason`. This is escalation-as-data (#231): the pipeline runs the
lifecycle branch and sets `needs_decision=True` only on `failed` / `abandoned`.
`complete` and `timeout` are clean terminals — `timeout` means the budget
elapsed with the jobs still live, so the caller just re-invokes to keep
watching.

## What stays in the LLM

The judgement that follows a `failed` run — read the failed tasks' stderr,
classify recoverable-vs-not via [failures](failures.md), then
[resubmit-failed](resubmit-failed.md) or [reconcile-journal](reconcile-journal.md)
— is NOT in the pipeline. `status-pipeline` flags it (`needs_decision=True`)
and hands back `last_status` / `failed_waves` as evidence, but does not itself
resubmit.

## Additive

This primitive does not replace the per-verb worker-prompt path; it is a new
verb the prompt may adopt. Nothing breaks if it is not yet wired in — which is
why it can ship before the prompt is restructured to call it.
