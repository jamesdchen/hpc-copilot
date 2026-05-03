---
name: hpc-submit
description: "Submit a parameter-grid experiment to a SLURM/SGE cluster via SSH and record it in the journal. End-to-end pipeline (rsync + deploy + qsub + record) in one CLI call."
allowed-tools: Bash Read Write
---

Agent-facing composition over the **[submit-flow](../../docs/primitives/submit-flow.md) workflow atom** (full pre-flight + rsync + deploy + qsub + record pipeline in one CLI call). For just the journal-write half (when the agent has already qsubbed), use the [submit-spec](../../docs/primitives/submit-spec.md) primitive directly. Both are idempotent on `run_id`: a replay returns `data.deduped: true` and emits no cluster-side side effects.

## Steps

1. Run `hpc-preflight` skill first if it has not already been run this session. Abort if it fails.

2. Confirm the target cluster exists via [clusters-describe](../../docs/primitives/clusters-describe.md); on `error_code: cluster_unknown`, list clusters and stop.

3. Discover executors via [discover-executors](../../docs/primitives/discover-executors.md) (optional, for grid construction). Parse `data.executors[]` for `name`, `path`, `cli_framework`, `has_compute_function`.

4. The parallelization axis lives in `.hpc/tasks.py` (`total()` + `resolve(task_id)`); the agent walks the user through writing it during `/submit` Step 6. Verify task count locally before submitting:

   ```bash
   python -c 'from hpc_mapreduce import load_tasks_module, tasks_path; print(load_tasks_module(tasks_path(".")).total())'
   ```

   If unexpectedly large (>1000), stop and surface to the caller.

5. **Construct the spec** per `submit-flow`'s `inputs:` contract (see `docs/primitives/submit-flow.md` — required fields + the `job_env` shape the cluster-side template expects). When invoked as one campaign iteration, set `campaign_id`. Stochastic strategies must include a unique `_optuna_trial_number`-style key in `tasks.resolve()` so each iteration's `cmd_sha` differs even when params repeat — see `hpc-campaign`.

6. **Dry-run** the workflow atom (`dry_run: true`); on `error_code: spec_invalid`, fix the spec and retry.

7. **Invoke** the workflow atom for real and parse the envelope. On `data.deduped: true`, switch to `hpc-status` (the original cluster jobs are already running — do NOT re-issue `submit-flow`). On `data.deduped: false`, record `run_id` + `job_ids` + (if present) `canary_run_id`/`canary_job_ids` for downstream calls.

8. **On error envelopes**, branch by the `error_code` table in `submit-flow`'s frontmatter (`spec_invalid` / `ssh_unreachable` / `remote_command_failed` / `internal`); each row carries `retry_safe` so the agent doesn't have to remember the policy.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or every cluster call hangs on auth. Run `hpc-preflight` first to catch this.
- **Scheduler rate limits**: serialize submits to a single cluster; most schedulers cap at ~1/sec. If submitting multiple jobs back-to-back, sleep 1s between calls or expect `scheduler_throttled`.
- **Idempotency**: `submit-flow` is replay-safe on `run_id`. If `data.deduped: true`, the original cluster jobs are already running — do NOT re-invoke. Resume monitoring instead via `hpc-status` or [monitor-flow](../../docs/primitives/monitor-flow.md).
- **No cancel/abort**: claude-hpc has no kill primitive. If you decide an experiment is bad, stop monitoring; cluster jobs run to walltime.
- `--dry-run` never touches the cluster and never writes to the journal — safe to run repeatedly.
