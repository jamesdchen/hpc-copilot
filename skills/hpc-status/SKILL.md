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
   - Snapshot: `hpc-mapreduce status --run-id <id>`. Returns immediately.
   - Wait-until-terminal: `hpc-mapreduce monitor-flow --spec foo.json` (with `run_id` + `wall_clock_budget_seconds`). Blocks until terminal/budget.

3. **Parse the envelope** per the chosen primitive's `outputs:` contract: both expose `lifecycle_state`, `last_status`, `combined_waves`, `failed_waves`. `monitor-flow` adds `ticks`, `elapsed_seconds`, `escalation_reason`.

4. **Decide next action** (this is the agent-specific layer):
   - `lifecycle_state == "complete"` — terminal; proceed to final aggregation via `hpc-aggregate`.
   - `lifecycle_state == "failed"` — surface to caller; consider [resubmit-failed](../../docs/primitives/resubmit-failed.md) (if recoverable) or [reconcile-journal](../../docs/primitives/reconcile-journal.md).
   - `lifecycle_state == "abandoned"` — recorded jobs no longer exist on the scheduler. Invoke [reconcile-journal](../../docs/primitives/reconcile-journal.md) to confirm.
   - `lifecycle_state == "in_flight"` (poll-run-status only) — caller waits and re-polls later.
   - `lifecycle_state == "timeout"` (monitor-flow only) — budget elapsed; cluster jobs continue; caller can re-invoke `monitor-flow` to keep watching.

5. **On error envelopes**, branch by `error_code` per the chosen primitive's frontmatter table.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or this call hangs on auth. Run `hpc-preflight` first.
- **Polling cadence for `poll-run-status`**: do NOT loop in tight cadence — sleep at least 60s between polls (300s for runs >30 min ETA). Schedulers and SSH multiplexers throttle aggressive polling. If you want a loop, use `monitor-flow` instead — it adapts cadence internally and writes one tick log entry per poll.
- **No cancel/abort**: claude-hpc has no kill primitive. Receiving `lifecycle_state == "in_flight"` for a bad experiment means the cluster jobs continue to walltime; the caller can stop monitoring but cannot terminate.
