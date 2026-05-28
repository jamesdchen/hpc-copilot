---
name: verify-submitted
verb: query
side_effects:
- ssh: <cluster> (scheduler state query)
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
backed_by:
  cli: hpc-agent verify-submitted [--experiment-dir <dir>] --run-id <run_id>
  python: hpc_agent.ops.verify_submitted.verify_submitted
---
# verify-submitted

Post-submit health check for a freshly-launched array. `qsub`/`sbatch`
returning a job id is necessary but not sufficient — an SGE array can sit in
`Eqw` (error) and a SLURM job can be held, both of which a plain alive-check
reports as merely "present." `verify-submitted` reads the run's `job_ids` from
the journal, queries per-job scheduler state over SSH (via the backend's
`build_scheduler_state_cmd`, routed through the `ssh_argv` seam), and returns
`{ok, states, healthy, error, held, missing, details}`. `ok` is true iff no job
is in an error or held state.

It exists so the submit worker's Step 8b is a verb call rather than raw
`ssh … qstat` (#157) — the procedure should never train agents to shell raw
ssh. Read-only: it never mutates the journal or the scheduler.
