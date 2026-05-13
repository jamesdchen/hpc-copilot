---
name: hpc-status
description: "Poll the status of an in-flight HPC run. Single snapshot via poll-run-status, or wait-until-terminal via monitor-flow."
allowed-tools: Bash Read Write
---

Agent-facing composition over two primitives that share the same observation surface but differ in scope:

- **One-shot snapshot** → invoke the [poll-run-status](../../docs/primitives/poll-run-status.md) primitive. Returns the current `last_status` and `lifecycle_state`. Use when the agent wants a single check ("is it done yet?") and will decide cadence itself.
- **Wait until terminal (or budget)** → invoke the [monitor-flow](../../docs/primitives/monitor-flow.md) workflow atom. Internal poll loop; auto-combines waves; returns when `lifecycle_state` reaches `complete`/`failed`/`abandoned` or `wall_clock_budget_seconds` elapses. Use when the agent wants to wait synchronously for a run to finish (the canonical campaign-loop case).

Both write the same journal `last_status` and the same `.monitor.jsonl` tick log; they're interchangeable views of the same operation.

## Steps

1. **If `run_id` is unknown**, invoke [list-in-flight](../../docs/primitives/list-in-flight.md) first; pick the matching `data.runs[].run_id` (filter by `profile`, `cluster`, or `submitted_at`).

2. **Pick the surface** based on the caller's need:
   - Snapshot: `hpc-agent status --run-id <id>`. Returns immediately. (The `status` subcommand is the CLI alias for the [poll-run-status](../../docs/primitives/poll-run-status.md) primitive.)
   - Wait-until-terminal: `hpc-agent monitor-flow --spec foo.json` (with `run_id` + `wall_clock_budget_seconds`). Blocks until terminal/budget.

3. **Parse the envelope** per the chosen primitive's `outputs:` contract: both expose `lifecycle_state`, `last_status`, `combined_waves`, `failed_waves`. `monitor-flow` adds `ticks`, `elapsed_seconds`, `escalation_reason`.

4. **Decide next action** based on `lifecycle_state`:
   - `complete` — terminal; proceed to final aggregation via `hpc-aggregate`.
   - `failed` — surface to caller; consider [resubmit-failed](../../docs/primitives/resubmit-failed.md) (if recoverable) or [reconcile-journal](../../docs/primitives/reconcile-journal.md).
   - `abandoned` — recorded jobs no longer exist on the scheduler. Invoke [reconcile-journal](../../docs/primitives/reconcile-journal.md) to confirm.
   - `in_flight` (poll-run-status only) — caller waits and re-polls later.
   - `timeout` (monitor-flow only) — budget elapsed; cluster jobs continue; caller can re-invoke `monitor-flow` to keep watching.

5. **On error envelopes**, branch by `error_code` per the chosen primitive's frontmatter table.

## Cadence + escalation rules (monitor-flow)

`monitor-flow` adapts cadence internally based on the run's age and recent state changes:

- Fresh runs (< 10 min since submit): poll every 30s.
- Mid-life runs (< 1h): poll every 60s.
- Long-running (> 1h): poll every 5 min, escalating to 15 min after 4h.
- After any wave completes: re-poll within 30s to start the combiner promptly.
- After 3 consecutive `unknown` task states: escalate via `escalation_reason: "n_unknown_runs_high"`.

The cadence parameters live on the spec's `wall_clock_budget_seconds` + the per-tier defaults in `monitor-flow`'s `outputs:` documentation. Override with `tick_interval_sec` if the caller has specific cadence requirements (rare).

## Resubmit decision flow

When `lifecycle_state == "failed"` with `failed_task_ids` non-empty:

1. Read the failed tasks' stderr tails via `poll-run-status` (the cluster-side reporter surfaces them in `data.tasks[<id>].err_log_path` for any task in `failed`/`unknown`).
2. Classify the failure via [failures](../../docs/primitives/failures.md). Recoverable categories (`oom_killed`, `cluster_timeout`, `node_failure`, `preempted`) → invoke [resubmit-failed](../../docs/primitives/resubmit-failed.md) with the matching category. Non-recoverable (`spec_invalid`, `executor_crash`) → surface to caller.
3. The auto-retry resolver in [resubmit-failed](../../docs/primitives/resubmit-failed.md) reads the run sidecar's `auto_retry` block to decide whether the resubmit is allowed (per-category `max_attempts`).

## Polling cadence (poll-run-status, manual loops)

If the agent is driving its own polling loop instead of using `monitor-flow`, do NOT loop in tight cadence — sleep at least 60s between polls (300s for runs >30 min ETA). Schedulers and SSH multiplexers throttle aggressive polling. If you want a loop, use `monitor-flow` instead — it adapts cadence internally and writes one tick log entry per poll.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or this call hangs on auth. Run `hpc-preflight` first.
- **No cancel/abort**: claude-hpc has no kill primitive. Receiving `lifecycle_state == "in_flight"` for a bad experiment means the cluster jobs continue to walltime; the caller can stop monitoring but cannot terminate.
- The journal `last_status` and the per-run `<run_id>.last_status.json` cache file both update on each `poll-run-status` call; the cache file's mtime tells the caller how stale the snapshot is.
