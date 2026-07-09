---
name: reconcile-stale
verb: mutate
side_effects:
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock) — terminal
    close for scheduler-unknown in-flight runs
- ssh: <cluster> (one scheduler-state query per login node, via batch-status)
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent reconcile-stale [--experiment-dir <dir>] [--now <now>] [--stale-after-hours
    <stale_after_hours>]
  python: hpc_agent.ops.monitor.reconcile_stale.reconcile_stale
---
# reconcile-stale

_Documentation pending._
