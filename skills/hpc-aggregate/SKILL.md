---
name: hpc-aggregate
description: "Finalize a run's aggregated metrics: combine all waves on cluster, pull partials locally, run reduce_partials, optionally pull summary files."
allowed-tools: Bash Read Write
---

Agent-facing composition over the **[aggregate-flow](../../docs/primitives/aggregate-flow.md) workflow atom** (ensure every wave is combined → rsync `_combiner/` partials locally → `reduce_partials` to produce the final aggregated metrics dict → optionally pull per-task summary files). For per-wave granularity (e.g. invoke combiner on a single wave during a stalled run), invoke the [combine-wave](../../docs/primitives/combine-wave.md) primitive directly. Idempotent on success per wave; failure is retry-safe via `combiner_max_retries`.

## Steps

1. **Verify the run is done** (or close enough). Invoke [poll-run-status](../../docs/primitives/poll-run-status.md); only proceed if `lifecycle_state` is `complete` (or the user explicitly wants a partial aggregate). For partial aggregation, pass `ensure_all_combined: false` to `aggregate-flow` to skip combining waves still in flight.

2. **Choose an output directory** if the atom's default (`<experiment-dir>/_aggregated/<run_id>/`) is not desired.

3. **Invoke** [aggregate-flow](../../docs/primitives/aggregate-flow.md) with `run_id`. Set `pull_summaries: true` + `summary_glob: "<pattern>"` if the caller needs per-task summary files locally.

4. **Parse the envelope** per the atom's `outputs:` contract: `aggregated_metrics` is the cross-wave reduced dict (keyed by run_id or grid-point); `combiner_dir_local` is where the partials landed; `summaries_dir_local` is set when `pull_summaries=true`; `waves_combined_this_call` reports which waves the atom combined this invocation (vs already-combined entering the call).

5. **On `escalation_reason` non-null**, the atom completed with at least one wave failing `combiner_max_retries`. Inspect `failed_waves`; the partial `aggregated_metrics` is what DID combine. Decide whether the partial result is acceptable, or invoke [combine-wave](../../docs/primitives/combine-wave.md) directly with `force=true` for the failed waves.

6. **On error envelopes**, branch by `error_code` per the atom's frontmatter (`journal_corrupt` / `spec_invalid` / `ssh_unreachable` / `remote_command_failed`).

7. **Profile-specific aggregate command**: when the per-run sidecar's `aggregate_defaults.aggregate_cmd` is set, run that ON THE CLUSTER after `aggregate-flow` returns — it's an arbitrary user-defined command that the framework doesn't introspect. Out of scope for this skill's automatic flow; surface to the caller.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or this call hangs on auth. Run `hpc-preflight` first.
- **Idempotency**: re-invoking `aggregate-flow` on the same `run_id` is safe and cheap. `combine-wave` skips already-combined waves; `rsync_pull` handles the diff; `reduce_partials` is a pure function over the pulled files.
- **No cancel/abort**: `combine-wave` runs the user's combiner script on the cluster; once started, it cannot be stopped from here. Set sensible walltimes in the combiner job itself.
- The CLI does NOT choose the combiner script or output schema; the user's repo provides `.hpc/_hpc_combiner.py`. This skill only orchestrates the call and records outcomes via the workflow atom.
