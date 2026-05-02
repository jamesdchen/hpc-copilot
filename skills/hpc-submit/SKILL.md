---
name: hpc-submit
description: "Submit a parameter-grid experiment to a SLURM/SGE cluster via SSH and record it in the journal."
allowed-tools: Bash Read Write
---

Submit a recorded run via the `hpc-mapreduce` CLI. The CLI is idempotent on `run_id`: a replay returns `data.deduped: true` and does NOT re-issue qsub.

## Smart-planning step (resource-quality aware)

Before constructing the spec, ask the planner for a per-candidate-constraint scorecard so the constraint and walltime aren't picked blind. This is the cost-model judgment step described in the design doc.

```bash
hpc-mapreduce plan-submit --profile <profile> --cluster <cluster> --experiment-dir <path>
```

The envelope's `data` is the planner output. Three branches:

1. **`needs_canary: true`** — no runtime priors exist for this `(profile, cluster)`. Read `data.canary_plan.constraint`, submit a single-task canary using that constraint, wait for it to complete, then ingest the elapsed time into `<repo>/.hpc/runtimes/<profile>.<cluster>.json` (use `hpc_mapreduce.job.runtime_prior.append_sample`). Re-call `plan-submit`. Now you have priors and the next call returns scored candidates.

2. **`needs_canary: false`** — score each candidate using:
   ```
   total_etc(c) = eta_sec(c) + p95_runtime(c) + p_fail(c) * (eta_sec(c) + p95_runtime(c))
   ```
   where `p95_runtime(c) = max(quantiles[gpu]['p95'] for gpu in c)` (worst-case among GPU types admitted by the constraint). Pick the candidate with smallest `total_etc`.

3. For each candidate's `stressed_nodes`, decide per-node whether to soft-exclude based on `co_tenants` context. Heuristic: a co-tenant job that has been running >12h with high CPU/mem share is unlikely to clear before our submit completes — exclude. Short / low-resource co-tenants are usually fine. Always exclude every entry in `blacklisted_nodes` (rule, not judgment).

4. Set `--time=` (walltime) to `chosen.p95_runtime * safety_margin` (default 1.3) so the budget covers the worst GPU type the constraint admits without ballooning.

5. Write the decision to `<experiment-dir>/.hpc/runs/<run_id>.decision.json` after sbatch returns, capturing the candidates considered and the chosen plan + Claude's free-form rationale. This is the audit trail.

If the planner errors (cluster unreachable, scheduler version skew, etc.), surface the error and fall back to the static-constraint flow described in the slash command.

## Steps

1. Run `hpc-preflight` skill first if it has not already been run this session. Abort if it fails.

2. Confirm the target cluster exists:
   ```bash
   hpc-mapreduce clusters describe <cluster_name>
   ```
   On `error_code: cluster_unknown`, list clusters and stop.

3. Discover executors in the experiment dir (optional, for grid construction):
   ```bash
   hpc-mapreduce discover --experiment-dir <path>
   ```
   Parse `data.executors[]` for `name`, `path`, `flags`.

4. The parallelization axis lives in `.hpc/tasks.py` (`total()` + `resolve(task_id)`); the agent walks the user through writing it during `/submit` Step 6. Verify task count locally before submitting:
   ```bash
   python -c 'from hpc_mapreduce import load_tasks_module, tasks_path; print(load_tasks_module(tasks_path(".")).total())'
   ```
   If unexpectedly large (>1000), stop and surface to the caller.

5. Write the submission spec to `spec.json` — required fields: `profile`, `cluster`, `ssh_target`, `remote_path`, `job_name`, `run_id`, `job_ids` (list), `total_tasks` (int). Construct `run_id` as `{profile}-{utc_ts}-{cmd_sha[:8]}` where `cmd_sha` comes from `hpc_mapreduce.compute_cmd_sha(load_tasks_module(tasks_path(".")))` — it ties identity to the materialized task list, so a re-run of an unchanged `.hpc/tasks.py` produces the same `run_id` and `submit` will dedup.

6. Dry-run validate first:
   ```bash
   hpc-mapreduce submit --spec spec.json --dry-run --experiment-dir <path>
   ```
   On `ok: true`, `data.would_launch` reports the task count. On `ok: false` with `error_code: spec_invalid`, fix the spec.

7. Submit for real:
   ```bash
   hpc-mapreduce submit --spec spec.json --experiment-dir <path>
   ```
   Parse the envelope:
   - `data.deduped: true` — a journal record for this `run_id` already exists. The original cluster jobs are already running. Do NOT re-issue qsub. Switch to `hpc-status` to monitor.
   - `data.deduped: false` — fresh submission. Record `data.run_id` and `data.job_ids` for downstream `status` / `aggregate` / `resubmit` calls.

8. On error envelopes, decide by `error_code`:
   - `ssh_unreachable` (category: network, retry_safe: true) — re-run preflight; retry after fix.
   - `scheduler_throttled` (cluster, retry_safe: true) — wait at least 1s, retry the same spec (idempotency protects against double-submit).
   - `spec_invalid` (user, retry_safe: false) — fix the spec; do not retry as-is.
   - `cluster_unknown` (user) — fix the cluster name in spec.
   - `internal` — surface to the caller; do not retry.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or every cluster call hangs on auth. Run `hpc-preflight` first to catch this.
- **Scheduler rate limits**: serialize submits to a single cluster; most schedulers cap at ~1/sec. If submitting multiple jobs back-to-back, sleep 1s between calls or expect `scheduler_throttled`.
- **Idempotency**: `hpc-mapreduce submit` is replay-safe on `run_id`. If the response has `data.deduped: true`, the original cluster jobs are already running — do NOT re-issue qsub. Resume monitoring instead.
- **No cancel/abort**: claude-hpc has no kill command. If you decide an experiment is bad, stop waiting; cluster jobs run to walltime.
- Exit codes: 0 ok, 1 user error (fix spec), 2 cluster/network (retry-safe per `retry_safe` field), 3 internal (surface).
- `--dry-run` never touches the cluster and never writes to the journal — safe to run repeatedly.
