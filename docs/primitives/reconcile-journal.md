---
name: reconcile-journal
verb: mutate
inputs:
- name: run_id
  type: string
- name: scheduler
  type: enum
  description: One of `sge`, `slurm`, `pbspro`, `torque`. Determines which on-cluster
    query backend to invoke.
- name: experiment_dir
  type: path
  description: Repo root. Defaults to cwd.
side_effects:
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)
- ssh: <cluster>
idempotent: true
idempotency_key: run_id
error_codes:
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: cluster_unknown
  category: user
  retry_safe: false
- code: journal_corrupt
  category: internal
  retry_safe: false
backed_by:
  cli: hpc-agent reconcile [--experiment-dir <dir>] --run-id <run_id> --scheduler
    <scheduler>
  python: hpc_agent.ops.monitor.reconcile.reconcile
exit_codes:
- 0: ok
- 2: ssh_unreachable / remote_command_failed
- 3: journal_corrupt
---

## Purpose

Self-healing resume. Re-derives ground truth for one run from the cluster (fresh status report, canonical `combined_waves` from `_combiner/wave_*.json`, alive job-ID check) and writes the merged result back atomically. If recorded `job_ids` are non-empty but none are alive on the scheduler, flips `lifecycle_state` to `abandoned`.

## Compose with

- Common predecessors: `list-in-flight` (to discover candidate run_ids).
- Common successors: `poll-run-status`, `combine-wave`, `resubmit-failed`, or terminal handling depending on the post-reconcile state.

## Notes

- Used by `/monitor-hpc`'s setup step before any other action — guards against the case where the local journal's `last_status` was set hours ago and the run silently completed (or got abandoned) since.
- Three parallel SSH calls means latency is dominated by the slowest of the three; agent should expect ~2-5s typical, longer on congested logins.
- Non-destructive in the sense that it never mutates cluster state — only the local journal record.
