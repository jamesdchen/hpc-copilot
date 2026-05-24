Agent-facing composition over two primitives that share the same observation surface but differ in scope:

- **One-shot snapshot** → invoke the [poll-run-status](../../docs/primitives/poll-run-status.md) primitive. Returns the current `last_status` and `lifecycle_state`. Use when the caller wants a single check ("is it done yet?") and will decide cadence itself.
- **Wait until terminal (or budget)** → invoke the [monitor-flow](../../docs/primitives/monitor-flow.md) workflow atom. Internal poll loop; auto-combines waves; returns when `lifecycle_state` reaches `complete`/`failed`/`abandoned` or `wall_clock_budget_seconds` elapses. Use when the caller wants to wait synchronously for a run to finish (the canonical campaign-loop case).

Both write the same journal `last_status` and the same `.monitor.jsonl` tick log; they're interchangeable views of the same operation.

## Step 0: Load context (run this first, every time)

Run `hpc-agent load-context --experiment-dir .` and treat its `data` as the ONLY source of truth for run / campaign state. Never rely on conversational memory or shell variables — a context compaction or a session restart erases them; the on-disk state does not.

- `data.in_flight` — active runs with `run_id`, `stage`, `ssh_target`, `job_ids`. This is the authoritative recovery path when `run_id` is unknown.
- `data.latest_run` — config snapshot of the newest run (cluster, profile, campaign_id).
- `data.next_step_hint` — `monitor` when a run is still in flight.

If a value you need is absent here, derive it from the run sidecar on disk — never from memory.

## Steps

1. **If `run_id` is unknown**, pick it from `data.in_flight` returned by Step 0 (filter by `profile`, `cluster`, or `submitted_at`). `list-in-flight` is the same data if you need a standalone call.

2. **Pick the surface** based on the caller's need:
   - Snapshot: `hpc-agent status --run-id <id>`. Returns immediately. (The `status` subcommand is the CLI alias for the [poll-run-status](../../docs/primitives/poll-run-status.md) primitive.)
   - Wait-until-terminal: `hpc-agent monitor-flow --spec foo.json` (with `run_id` + `wall_clock_budget_seconds`). Blocks until terminal/budget.

3. **Parse the envelope** per the chosen primitive's `outputs:` contract: both expose `lifecycle_state`, `last_status`, `combined_waves`, `failed_waves`. `monitor-flow` adds `ticks`, `elapsed_seconds`, `escalation_reason`.

4. **Decide next action** based on `lifecycle_state`:
   - `complete` — terminal; the caller proceeds to final aggregation via the aggregate workflow.
   - `failed` — record in `decisions`; the caller may invoke [resubmit-failed](../../docs/primitives/resubmit-failed.md) (if recoverable) or [reconcile-journal](../../docs/primitives/reconcile-journal.md).
   - `abandoned` — recorded jobs no longer exist on the scheduler. Invoke [reconcile-journal](../../docs/primitives/reconcile-journal.md) to confirm.
   - `in_flight` (poll-run-status only) — caller waits and re-polls later.
   - `timeout` (monitor-flow only) — budget elapsed; cluster jobs continue; caller can re-invoke `monitor-flow` to keep watching.

5. **On error envelopes**, branch by `error_code` per the chosen primitive's frontmatter table.

## Cadence + escalation (monitor-flow)

`monitor-flow` adapts its poll cadence internally — by run age, on wave completion, and on repeated `unknown` task states — and surfaces `escalation_reason` when it escalates. The caller does not manage cadence; it just reads the returned `escalation_reason` and branches per the rules above. The tier table and the `tick_interval_sec` override are in [monitor-flow.md](../../docs/primitives/monitor-flow.md).

## Resubmit decision flow

When `lifecycle_state == "failed"` with `failed_task_ids` non-empty:

1. Read the failed tasks' stderr tails via `poll-run-status` (the cluster-side reporter surfaces them in `data.tasks[<id>].err_log_path` for any task in `failed`/`unknown`).
2. Classify the failure via [failures](../../docs/primitives/failures.md). Recoverable categories (`oom_killed`, `cluster_timeout`, `node_failure`, `preempted`) → invoke [resubmit-failed](../../docs/primitives/resubmit-failed.md) with the matching category. Non-recoverable (`spec_invalid`, `executor_crash`) → record in `decisions` and stop.
3. The auto-retry resolver in [resubmit-failed](../../docs/primitives/resubmit-failed.md) reads the run sidecar's `auto_retry` block to decide whether the resubmit is allowed (per-category `max_attempts`).

## Polling cadence (poll-run-status, manual loops)

If the caller is driving its own polling loop instead of using `monitor-flow`, do NOT loop in tight cadence — sleep at least 60s between polls (300s for runs >30 min ETA). Schedulers and SSH multiplexers throttle aggressive polling. If you want a loop, use `monitor-flow` instead — it adapts cadence internally and writes one tick log entry per poll.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or this call hangs on auth. The user runs `hpc-agent setup --cluster <name>` once per machine to probe the environment before submitting.
- **No cancel/abort**: hpc-agent has no kill primitive. Receiving `lifecycle_state == "in_flight"` for a bad experiment means the cluster jobs continue to walltime; the caller can stop monitoring but cannot terminate.
- The journal `last_status` and the per-run `<run_id>.last_status.json` cache file both update on each `poll-run-status` call; the cache file's mtime tells the caller how stale the snapshot is.
