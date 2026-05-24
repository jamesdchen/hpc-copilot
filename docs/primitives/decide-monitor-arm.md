---
name: decide-monitor-arm
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent decide-monitor-arm --spec <path>
  python: hpc_agent.ops.monitor.arm.decide_monitor_arm
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Pick the cron / loop / none arm mode + cadence + cron schedule string for a `/monitor-hpc` tick. Replaces three slash-command failure modes at once: arm choice, cadence selection, and cron schedule formatting.

The agent's job collapses to: read the run state, call this primitive, and (when `arm == "cron"`) pass `data.cron_create_args` directly to the `CronCreate` Claude Code tool to schedule the next monitor tick.

## Compose with

- **Predecessors**: `monitor-flow` (writes the journal record + tick log this primitive reads from), `monitor-summary` (terminal-state framing, called in `/monitor-hpc` Step 7).
- **Successors**: `CronCreate` (Claude Code tool — outside this catalog) when `arm == "cron"`. Otherwise the slash command exits and the next tick fires from the same cron.

## Notes

- **Adaptive table**: built into the primitive, lifted from `/monitor-hpc` Step 5's Markdown table. Order: queue-wait super-cache → ETA branches → all-pending fallback → running fallback. Single source of truth so changes land in one place.
- **Cron-driven self-scheduling**: `data.cron_create_args` is ready to pass to `CronCreate`; each tick is a fresh process, so there is no exit contract to satisfy. The cadence table also picks a sensible interval for a cron running the headless `hpc-campaign-driver`.
- **Terminal detection**: `arm == "none"` when (a) `complete == total_tasks` or (b) `failed > 0 and running == 0 and pending == 0`. The slash command must `CronDelete` any prior cron for the run_id when arm is none.
- **`/loop` invocation**: when `user_invoked_via_loop=True`, returns `arm == "loop"` with `cadence_sec=0` and `cron_create_args=null`. The user is driving the cadence, so no cron is registered.
- **Side-effect-free**: pure function. Safe to call from anywhere (slash command, external orchestrator, debug shell). Run multiple times to compare cadence picks across hypothetical states.
