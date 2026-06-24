---
name: batch-status
verb: query
side_effects:
- ssh: <cluster> (one scheduler-state query per login node)
idempotent: true
idempotency_key: none
error_codes:
- code: ssh_unreachable
  category: network
  retry_safe: true
backed_by:
  cli: hpc-agent batch-status [--experiment-dir <dir>]
  python: hpc_agent.ops.monitor.batch_status.batch_status
---
# batch-status

Poll **all** in-flight runs with one scheduler query per login node, instead of
one query per run.

`batch-status` reads the journal's in-flight runs, groups them by
`(ssh_target, scheduler)`, and issues a single `qstat -u $USER` / `squeue` per
login node — then distributes the parsed `TaskStatus` values back to each run. N
runs on one cluster cost **one** scheduler connection per tick rather than N,
which keeps a monitoring loop from bursting connections past a login node's
fail2ban / rate limiter (the Nextflow/Parsl "query the scheduler once for all
jobs" pattern).

Read-only: it queries scheduler state and returns it; it never mutates the
journal or the cluster. An unreachable login node surfaces as `ssh_unreachable`
(retry-safe) for that node's group while other groups still report.

**Returns** `{runs: {run_id: {job_states, missing_job_ids}}, queries, skipped}`
— `missing_job_ids` are ids absent from the live queue (a finished job leaves
the queue, so the caller infers completion from absence). The SSH transport seam
is `hpc_agent.infra.cluster_status.ssh_batch_scheduler_states`; per-family token
mapping lives in `HPCBackend.batch_status`.
