Agent-facing composition over the **[aggregate-flow](../../docs/primitives/aggregate-flow.md) workflow atom** (ensure every wave is combined → rsync `_combiner/` partials locally → `reduce_partials` to produce the final aggregated metrics dict → optionally pull per-task summary files). For per-wave granularity (e.g. invoke combiner on a single wave during a stalled run), invoke the [combine-wave](../../docs/primitives/combine-wave.md) primitive directly. Idempotent on success per wave; failure is retry-safe via `combiner_max_retries`.

## Reporting conventions

Two fields on the worker report carry observations back to the caller — they are NOT interchangeable:

- **`decisions`** is the **strict enumerated record** of which judgement points this workflow reached. For the **aggregate** workflow there are exactly four allowed `point` IDs — any other value is rejected by `parse_worker_report`:
  - `mode` (backed by `aggregate-flow` — auto / combiner-only)
  - `partial_handling` (backed by `decide-partial-handling` — proceed on incomplete waves or not)
  - `completeness` (backed by `verify-aggregation-complete`)
  - `reduce_locality` (backed by `aggregate-flow` — deterministic: `mode=auto` reduces where the data sits)

  Each entry is `{point, outcome, why, chosen?, rejected?}` — `outcome` is a short tag (e.g. `unexpected_tasks`, `partial`, `manual_pending`). At a **judgement** point (a genuine control-flow branch the deterministic layer could not decide for you — here `partial_handling`), `why` is **required** (`parse_worker_report` rejects an empty one), and you should set `chosen` (the branch taken) and `rejected` (the alternatives you weighed and discarded). At a deterministic point `why` is a free-form one-liner.

- **`anomalies`** is a **free-form multi-line string** for everything else: the specific violation lists (`missing_waves` / `missing_tasks` / `unexpected_tasks` / `provenance_present`), failed-wave ids, raw evidence — anything that isn't one of the four points.

When in doubt, prefer `anomalies`. **Do not invent new `decisions` point IDs** (`unexpected_tasks_present`, `partial_aggregate`, `manual_aggregate_pending` are *outcomes*, not points) — the envelope is rejected and the run reports as broken even when the aggregation succeeded.

## Step 0: Load context (run this first, every time)

Run `hpc-agent load-context --experiment-dir .` and treat its `data` as the ONLY source of truth for run / campaign state. Never rely on conversational memory or shell variables — a context compaction or a session restart erases them; the on-disk state does not.

- `data.in_flight` — active runs with `run_id`, `ssh_target`, `remote_path`. `aggregate-flow` reads these from the journal itself; you pass it `run_id`, not connection details.
- `data.latest_run` — config snapshot of the newest run, including `result_dir_template`.

If a value you need is absent here, derive it from the run sidecar on disk — never from memory.

## Steps

1. **Verify the run is done.** `aggregate-flow` gates on the journal's terminal `status` — aggregating a non-terminal run risks reducing over partial data and reporting plausible-but-wrong metrics. The journal reaches terminal one of two ways:
   - **monitor-flow already ran it to terminal** (the normal path) — its poll loop calls `mark-run-terminal` when the cluster confirms completion. Then aggregate's gate passes; proceed.
   - **The caller skipped monitor on a short run** — the journal still says `in_flight`. Pass `reconcile_terminal: true` to `aggregate-flow` (Step 5): it polls the cluster ONCE and, if the run is confirmed done, marks the journal terminal (via the same `mark-run-terminal` atom monitor uses) before the gate. If the cluster shows work still in flight, the gate still fires — aggregate never reconciles a genuinely-running run.

   `poll-run-status` alone is **not** sufficient here: it refreshes the snapshot `last_status` but does NOT drive the lifecycle `status` to terminal. For a deliberate partial aggregate of a still-running run, pass `ensure_all_combined: false` instead (skips both the gate and combining waves still in flight).

2. **No manual sidecar pull.** `aggregate-flow` self-sources the per-run sidecar — it reads the cluster's wave partials directly and SSH-reads the remote sidecar for `aggregate_defaults` when the local copy is absent. Do **not** `rsync` the sidecar by hand; the worker reaches the cluster only through `hpc-agent`. (`.hpc/tasks.py` is git-tracked and already in your repo.)

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
     "pull_summaries": false,
     "reconcile_terminal": false
   }
   ```

   Set `reconcile_terminal: true` only on the skip-monitor path (Step 1) — when the journal still says `in_flight` because no monitor-flow drove this run to terminal.

   ```bash
   hpc-agent aggregate-flow --spec spec.json --experiment-dir .
   ```

   **Shortcut** — when every other field is at its default (the
   defaults shown above ARE the defaults), drop the spec file and pass
   `--run-id` instead:

   ```bash
   hpc-agent aggregate-flow --run-id <run_id> --experiment-dir .
   ```

   `--run-id` and `--spec` are mutually exclusive. Use `--spec` when
   you need to override any field (e.g. `pull_summaries: true`,
   `mode: "combiner-only"`, `min_rows: 1`).

6. **Parse the envelope** per the atom's `outputs:` contract: `aggregated_metrics` is the cross-wave reduced dict (keyed by run_id or grid-point); `combiner_dir_local` is where the partials landed; `summaries_dir_local` is set when `pull_summaries=true`; `waves_combined_this_call` reports which waves the atom combined this invocation (vs already-combined entering the call).

7. **Verify framework-knowable invariants** before reporting to the caller. **Required precondition:** `$COMBINER_DIR_LOCAL` must already hold the directory `aggregate-flow` returned in step 6's envelope (`combiner_dir_local`). If `aggregate-flow` errored (step 9 branched first), or step 6 didn't populate `combiner_dir_local`, **STOP** — record the missing/erroring `aggregate-flow` context in `anomalies` and report. Do NOT invoke `verify-aggregation-complete` with an empty / unset `--combiner-dir` — that's a guaranteed false negative, not a verification. Invoke [verify-aggregation-complete](../../docs/primitives/verify-aggregation-complete.md):

   ```bash
   hpc-agent verify-aggregation-complete \
       --experiment-dir . \
       --run-id "$RUN_ID" \
       --combiner-dir "$COMBINER_DIR_LOCAL"
   ```

   The envelope's `data` carries `{ok, all_waves_combined, missing_waves, all_tasks_present, missing_tasks, unexpected_tasks, provenance_present, ...}`. Branch:
   - `ok=True` → proceed to interpretation.
   - `ok=False` → record a `completeness` decision with outcome `failed` and put the specific violations (`missing_waves` / `missing_tasks` / `unexpected_tasks` / `provenance_present`) in `anomalies` before any user-facing framing. `unexpected_tasks` in particular is a cross-run contamination red flag — record it as a `completeness` decision with outcome `unexpected_tasks` (the ids go in `anomalies`), never paper over.

8. **On `escalation_reason` non-null** in the aggregate-flow envelope, the atom completed with at least one wave failing `combiner_max_retries`. Don't eyeball it — call [decide-partial-handling](../../docs/primitives/decide-partial-handling.md) with `--failed-count` (len `failed_waves`), `--combined-count` (len `combined_waves`), and `--retries-exhausted` (set, since these failed `combiner_max_retries`). On `decided_by="code"` it resolved `retry`/`proceed` — follow it (for `retry`, invoke [combine-wave](../../docs/primitives/combine-wave.md) with `force=true` for the failed waves). On `decided_by="judgement"` it returns the computed `missing_fraction` and the only open call is acceptability *for your purpose*: record a `partial_handling` decision choosing `accept-partial` vs `force-retry-failed` with `chosen`/`rejected`/`why` (put the failed-wave list in `anomalies`).

9. **On error envelopes**, branch by `error_code` per the atom's frontmatter (`journal_corrupt` / `spec_invalid` / `ssh_unreachable` / `remote_command_failed`).

10. **Profile-specific aggregate command**: when the per-run sidecar's `aggregate_defaults.aggregate_cmd` is set and `mode != "auto"` skipped it, the atom already ran the user-defined cluster-side command. When `mode == "combiner-only"` was forced and the caller still wants the cluster-side command, record a `mode` decision with outcome `manual_pending` (note the pending command in `anomalies`) — it's an arbitrary user-defined command that the framework doesn't introspect.

## Reduce where the data lives (why `mode: "auto"` is the default)

You never move bulk result files yourself — `aggregate-flow` does all cluster I/O internally. Your only lever is the spec, and the principle behind it is: reduce where the data already sits, pull only the small result. That is exactly what `mode: "auto"` does — it routes to `cluster-reduce` (run the user's reducer on the cluster, pull the single KB-sized JSON output) when an `aggregate_cmd` is available, and only falls back to combiner-only otherwise.

So the decision encoded in the spec is:

1. **Genuinely HPC-scale or bulk data** → stay on `mode: "auto"`; the reduction runs cluster-side and only the reduced output comes back. This is the 90% case.
2. **You need the raw per-task files local** (debug, manual interpretation) → set `pull_summaries: true` with an explicit `summary_glob`. Opt-in precisely because pulling thousands of small files is slow.

Don't override `mode` to force a local pull of bulk partials to reach a Python environment — that's the anti-pattern `cluster-reduce` exists to prevent. If the cluster-side reducer lacks a dependency, that's a fix to the user's cluster environment (their combiner/reducer script), surfaced via the envelope — not a reason to drag the data to where the deps are.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or this call hangs on auth. The user runs `hpc-agent setup --cluster <name>` once per machine to probe the environment before submitting.
- **Idempotency**: re-invoking `aggregate-flow` on the same `run_id` is safe and cheap. `combine-wave` skips already-combined waves; `rsync_pull` handles the diff; `reduce_partials` is a pure function over the pulled files.
- **No cancel/abort**: `combine-wave` runs the user's combiner script on the cluster; once started, it cannot be stopped from here. Set sensible walltimes in the combiner job itself.
- **CLI does NOT choose the combiner script or output schema.** The user's repo provides `.hpc/_hpc_combiner.py`. This procedure only orchestrates the call and records outcomes via the workflow atom.
- **`mode: "auto"` is load-bearing.** It's what makes `cluster-reduce` (small JSON output) the default route and `combiner-only + pull_summaries=true` (raw per-task files) opt-in. Don't override unless the caller has a specific reason.
