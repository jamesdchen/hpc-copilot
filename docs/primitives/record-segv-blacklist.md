---
name: record-segv-blacklist
verb: mutate
inputs:
  - name: experiment_dir
    type: path
  - name: cluster
    type: string
  - name: node
    type: string
    description: Hostname of the node that SEGV'd.
  - name: run_id
    type: string
  - name: job_id
    type: string
  - name: task_id
    type: int
  - name: exit_code
    type: int
  - name: signal
    type: int
    description: Signal number (e.g. -11 for SIGSEGV).
  - name: host_allocmem_pct
    type: float
    description: Optional context from inspect-cluster at SEGV time.
    default: null
  - name: cpu_load_frac
    type: float
    default: null
  - name: concurrent_jobs
    type: list[string]
    default: []
outputs:
  - name: written
    type: bool
side_effects:
  - mutates: <experiment_dir>/.hpc/blacklist/<cluster>.json (atomic; appends or refreshes one entry under flock)
idempotent: true
idempotency_key: (cluster, node) — repeated SEGVs on the same node refresh the entry's TTL rather than appending duplicates
error_codes:
  - code: internal
    category: internal
    retry_safe: false
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_mapreduce.job.blacklist.record_segv
exit_codes:
  - n/a (Python-only)
---

## Purpose

Append (or refresh) one entry on the per-cluster SEGV blacklist so future `score-submit-plan` calls always-exclude the node. Each entry has a 7-day TTL by default; repeated SEGVs on the same node refresh it without duplicating.

## Compose with

- Common predecessors: `poll-run-status` surfacing a SEGV-class failure, plus `inspect-cluster` (to capture per-node context like `alloc_mem_pct` and `co_tenants` at the moment of failure — useful for post-hoc analysis of which contention patterns predict SEGVs).
- Common successors: `resubmit-failed` (with `category: segv`) on a different node.

## Notes

- The entry is per-project (lives under `<experiment_dir>/.hpc/blacklist/`), not global — a node that's bad for one workload may be fine for another.
- **Do not clear blacklist entries automatically.** The 7-day TTL is the only auto-expiry; manual cleanup is the user's call.
- The next `score-submit-plan` invocation will surface `blacklisted_nodes` per candidate so the planner can score around them.
