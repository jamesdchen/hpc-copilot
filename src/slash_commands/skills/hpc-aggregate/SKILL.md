---
name: hpc-aggregate
description: "Finalize a run's aggregated metrics: combine all waves on cluster, pull partials locally, run reduce_partials, optionally pull summary files."
allowed-tools: Bash Read Write Task
execution: delegated
---

Agent-facing composition over the **[aggregate-flow](../../docs/primitives/aggregate-flow.md) workflow atom** (ensure every wave is combined → rsync `_combiner/` partials locally → `reduce_partials` to produce the final aggregated metrics dict → optionally pull per-task summary files). For per-wave granularity (e.g. invoke combiner on a single wave during a stalled run), invoke the [combine-wave](../../docs/primitives/combine-wave.md) primitive directly. Idempotent on success per wave; failure is retry-safe via `combiner_max_retries`.

## Step 0: Load context (run this first, every time)

Run `hpc-agent load-context --experiment-dir .` and treat its `data` as the ONLY source of truth for run / campaign state. Never rely on conversational memory or shell variables — a context compaction or a session restart erases them; the on-disk state does not.

- `data.in_flight` — active runs with `run_id`, `ssh_target`, `remote_path`. These resolve the `$SSH_TARGET` / `$REMOTE_PATH` / `<run_id>` used in Step 2's rsync.
- `data.latest_run` — config snapshot of the newest run, including `result_dir_template`.

If a value you need is absent here, derive it from the run sidecar on disk — never from memory.

## Delegating aggregation to a subagent

Aggregation can pull large partial sets and emit a sizable `aggregated_metrics` dict. When you run this skill as part of a larger flow, do Steps 1–9 inside a fresh-context **subagent** (the `Task` tool) that returns **only** the `aggregate-flow` output envelope — `{ok, aggregated_metrics summary, missing_waves, missing_tasks, escalation_reason}` — and a single free-text `anomalies` string for anything off-contract. No transcript, no raw output: the `_combiner/` pull and per-task output stay in the subagent's context. The orchestrator parses fields, not prose; that field-shaped return is what keeps its next call deterministic. The subagent opens by running `hpc-agent load-context` to recover the `run_id` and SSH target.

## Steps

1. **Verify the run is done** (or close enough). Invoke [poll-run-status](../../docs/primitives/poll-run-status.md); only proceed if `lifecycle_state` is `complete` (or the user explicitly wants a partial aggregate). For partial aggregation, pass `ensure_all_combined: false` to `aggregate-flow` to skip combining waves still in flight.

2. **Pull the run sidecar locally if missing** so `aggregate-flow` can read its `wave_map`, `result_dir_template`, `task_count`, and (if set) `aggregate_defaults`:

   ```bash
   mkdir -p .hpc/runs
   rsync -az $SSH_TARGET:$REMOTE_PATH/.hpc/runs/<run_id>.json ./.hpc/runs/<run_id>.json
   ```

   `.hpc/tasks.py` is git-tracked locally; it should already be in your repo.

3. **Choose a mode**. The default is `mode: "auto"` and the right choice 90% of the time — it routes to `cluster-reduce` when the sidecar's `aggregate_defaults.aggregate_cmd` is set; otherwise to combiner-only. Overrides:
   - `mode: "cluster-reduce"` — force the cluster-side reducer; raise if no `aggregate_cmd` is available.
   - `mode: "combiner-only"` — bypass the reducer; pull `_combiner/` partials and reduce locally. Useful when `metrics.json` already carries the right per-task scalar.

4. **Choose `pull_summaries`**. Default `false` — only enable it (with an explicit `summary_glob`) when the caller genuinely needs raw per-task files locally for debug or interpretation. Keeping the default off is what avoids the bulk-pull anti-pattern that triggered the `cluster-reduce` primitive in the first place.

5. **Invoke** [aggregate-flow](../../docs/primitives/aggregate-flow.md) with `run_id`. Spec shape:

   ```json
   {
     "run_id": "<run_id>",
     "ensure_all_combined": true,
     "combiner_max_retries": 1,
     "mode": "auto",
     "pull_summaries": false
   }
   ```

   ```bash
   hpc-agent aggregate-flow --spec spec.json --experiment-dir .
   ```

6. **Parse the envelope** per the atom's `outputs:` contract: `aggregated_metrics` is the cross-wave reduced dict (keyed by run_id or grid-point); `combiner_dir_local` is where the partials landed; `summaries_dir_local` is set when `pull_summaries=true`; `waves_combined_this_call` reports which waves the atom combined this invocation (vs already-combined entering the call).

7. **Verify framework-knowable invariants** before reporting to the caller. Invoke [verify-aggregation-complete](../../docs/primitives/verify-aggregation-complete.md):

   ```bash
   hpc-agent verify-aggregation-complete \
       --experiment-dir . \
       --run-id "$RUN_ID" \
       --combiner-dir "$COMBINER_DIR_LOCAL"
   ```

   The envelope's `data` carries `{ok, all_waves_combined, missing_waves, all_tasks_present, missing_tasks, unexpected_tasks, provenance_present, ...}`. Branch:
   - `ok=True` → proceed to interpretation.
   - `ok=False` → surface the specific violations (`missing_waves` / `missing_tasks` / `unexpected_tasks` / `provenance_present`) before any user-facing framing. `unexpected_tasks` in particular is a cross-run contamination red flag — escalate, don't paper over.

8. **On `escalation_reason` non-null** in the aggregate-flow envelope, the atom completed with at least one wave failing `combiner_max_retries`. Inspect `failed_waves`; the partial `aggregated_metrics` is what DID combine. Decide whether the partial result is acceptable, or invoke [combine-wave](../../docs/primitives/combine-wave.md) directly with `force=true` for the failed waves.

9. **On error envelopes**, branch by `error_code` per the atom's frontmatter (`journal_corrupt` / `spec_invalid` / `ssh_unreachable` / `remote_command_failed`).

10. **Profile-specific aggregate command**: when the per-run sidecar's `aggregate_defaults.aggregate_cmd` is set and `mode != "auto"` skipped it, the atom already ran the user-defined cluster-side command. When `mode == "combiner-only"` was forced and the user still wants the cluster-side command, run it manually after `aggregate-flow` returns — it's an arbitrary user-defined command that the framework doesn't introspect.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or this call hangs on auth. Run `hpc-preflight` first.
- **Idempotency**: re-invoking `aggregate-flow` on the same `run_id` is safe and cheap. `combine-wave` skips already-combined waves; `rsync_pull` handles the diff; `reduce_partials` is a pure function over the pulled files.
- **No cancel/abort**: `combine-wave` runs the user's combiner script on the cluster; once started, it cannot be stopped from here. Set sensible walltimes in the combiner job itself.
- **CLI does NOT choose the combiner script or output schema.** The user's repo provides `.hpc/_hpc_combiner.py`. This skill only orchestrates the call and records outcomes via the workflow atom.
- **`mode: "auto"` is load-bearing.** It's what makes `cluster-reduce` (small JSON output) the default route and `combiner-only + pull_summaries=true` (raw per-task files) opt-in. Don't override unless the caller has a specific reason.
