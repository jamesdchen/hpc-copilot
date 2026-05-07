---
name: cluster-reduce
verb: mutate
side_effects:
- ssh: <cluster> (run reducer)
- rsync-pull: <remote_path>/<output_rel> → <local_dir>
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: true
backed_by:
  cli: hpc-agent cluster-reduce --experiment-dir <path> --run-id <id> [--aggregate-cmd
    <cmd>]
  python: claude_hpc.atoms.cluster_reduce.cluster_reduce
exit_codes:
- 0: ok
- 1: user-error
- 2: cluster
---

## Purpose

Run the user's reducer on the cluster, pull only its single JSON output. Eliminates the bulk per-task `rsync_pull` failure mode where `aggregate-flow` with `pull_summaries=True` + a permissive `summary_glob` drags every per-task output file (often thousands of CSVs / pickles) to the local machine before reducing.

The contract: any program that accepts `$HPC_RUN_ID` (or `--run-id`) and writes a single JSON file to `$HPC_AGGREGATED_OUTPUT` (default `_aggregated/<run_id>.json` under `remote_path`) is a valid reducer. See [reducer-contract.md](../reference/reducer-contract.md) for the full surface.

## Compose with

- **Predecessors**: `monitor-flow` (waits for terminal); `discover-reducers` (finds the reducer module path on the cluster repo).
- **Successors**: `verify-aggregation-complete` (still useful — it walks the on-cluster `_combiner/` partials independently of the reducer's output).
- **Often replaces**: the `pull_summaries=True` path in `aggregate-flow`. `aggregate-flow`'s new `mode='cluster-reduce'` (or `mode='auto'` with `aggregate_cmd` set) routes here automatically.

## Notes

- **`reduced` is the parsed JSON.** The agent doesn't need to re-read the local copy; the envelope's `data.reduced` is the dict (or list/scalar) the reducer wrote.
- **Output path is configurable.** `--output-path "custom/{run_id}.summary.json"` substitutes `{run_id}` and uses that path on the cluster (relative to `remote_path` or absolute). The local pull lands the same basename in `--local-dir`.
- **`extra_env`** lets the caller forward additional env vars to the reducer. Use this for dataset paths, debug flags, etc. that the reducer needs but aren't part of the framework contract.
- **Default 30-minute timeout.** A reducer over 1200 chunks should typically run in seconds; the budget exists for outliers (large CSVs, slow filesystem).
