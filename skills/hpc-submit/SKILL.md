---
name: hpc-submit
description: "Submit a parameter-grid experiment to a SLURM/SGE cluster via SSH and record it in the journal."
allowed-tools: Bash Read Write
---

Submit a recorded run via the `hpc-mapreduce` CLI. The CLI is idempotent on (profile, manifest_sha): a replay returns `data.deduped: true` and does NOT re-issue qsub.

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

4. Optionally expand the grid to verify task count before submitting:
   ```bash
   hpc-mapreduce expand-grid --spec spec.json --experiment-dir <path>
   ```
   Read `data.total`. If unexpectedly large (>1000), stop and surface to the caller.

5. Write the submission spec to `spec.json` — required fields: `profile`, `cluster`, `ssh_target`, `remote_path`, `job_name`, `manifest_filename`, `job_ids` (list), `total_tasks` (int). Optional: `run_id` (for explicit dedup).

6. Dry-run validate first:
   ```bash
   hpc-mapreduce submit --spec spec.json --dry-run --experiment-dir <path>
   ```
   On `ok: true`, `data.would_launch` reports the task count. On `ok: false` with `error_code: manifest_invalid`, fix the spec.

7. Submit for real:
   ```bash
   hpc-mapreduce submit --spec spec.json --experiment-dir <path>
   ```
   Parse the envelope:
   - `data.deduped: true` — a journal record for this (profile, manifest_sha) already exists. The original cluster jobs are already running. Do NOT re-issue qsub. Switch to `hpc-status` to monitor.
   - `data.deduped: false` — fresh submission. Record `data.run_id` and `data.job_ids` for downstream `status` / `aggregate` / `resubmit` calls.

8. On error envelopes, decide by `error_code`:
   - `ssh_unreachable` (category: network, retry_safe: true) — re-run preflight; retry after fix.
   - `scheduler_throttled` (cluster, retry_safe: true) — wait at least 1s, retry the same spec (idempotency protects against double-submit).
   - `manifest_invalid` (user, retry_safe: false) — fix the spec; do not retry as-is.
   - `cluster_unknown` (user) — fix the cluster name in spec.
   - `internal` — surface to the caller; do not retry.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or every cluster call hangs on auth. Run `hpc-preflight` first to catch this.
- **Scheduler rate limits**: serialize submits to a single cluster; most schedulers cap at ~1/sec. If submitting multiple jobs back-to-back, sleep 1s between calls or expect `scheduler_throttled`.
- **Idempotency**: `hpc-mapreduce submit` is replay-safe on (profile, manifest_sha). If the response has `data.deduped: true`, the original cluster jobs are already running — do NOT re-issue qsub. Resume monitoring instead.
- **No cancel/abort**: claude-hpc has no kill command. If you decide an experiment is bad, stop waiting; cluster jobs run to walltime.
- Exit codes: 0 ok, 1 user error (fix spec), 2 cluster/network (retry-safe per `retry_safe` field), 3 internal (surface).
- `--dry-run` never touches the cluster and never writes to the journal — safe to run repeatedly.
