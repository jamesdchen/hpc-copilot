`/monitor-hpc` is the **human-interview wrapper** around the `hpc-status` skill — the block-loop relay that starts with `status-snapshot` (a cheap journal-first digest of what is running where and what changed since the user last looked) and, for a live run, watches to terminal via the `status-watch` block, which is **detach-by-contract** (connection-broker.md 2026-07-07): it spawns a durable background worker that owns the one cold dial to terminal and returns a handle immediately, so no unattended tick dials the cluster inline. The slash parses arguments, invokes the skill, and relays each brief.

## The flow

1. **Parse `$ARGUMENTS`** — an optional `run_id`, and whether the user asked to wait until done (`wait_terminal`).
2. **Invoke the skill.** It runs `status-snapshot` and hands back the digest brief.
3. **Relay the brief, collect `y` or a nudge.** Show `running_where` / `changed_since_seen` and any `anomalies` or `stalled_runs`, plus the `next_block` suggestion. For a live run the suggestion is `status-watch`; the user greenlights it with a `y` or nudges. There are no per-field `[Y/n]` dialogs — the brief carries the recommendation DATA.
4. **Loop.** On `y`, the skill journals the greenlight and fires `status-watch`, which detaches a durable worker and returns a handle immediately; the skill awaits the detached worker and, on its exit, reads the terminal/anomaly brief from ONE `block-drive` tick (the recorded-terminal replay — see the hpc-status skill's never-stall rule). A clean `complete` hands off to the harvest block (`submit-s4` / `/aggregate-hpc`).

## Invocation

Invoke the `hpc-status` skill via the Skill tool:

```
Skill("hpc-status", {
  experiment_dir: ".",
  run_id: <if user stated>,
  wait_terminal: <true if the user said "wait until done", else omit>
})
```

## Relaying a brief

- **Multiple in-flight runs:** the snapshot brief lists them (grouped by `campaign_id` when ≥3). Ask which run and fold it into the nudge.
- **A failure / anomaly brief** carries the code-classified error and a structured `recommendation` (classify-then-resubmit for a failed run; reconcile-then-confirm before resubmit for an abandoned one). Surface it and collect `y` or a nudge — the default is never silent auto-resubmit (re-running the same bug wastes cluster time).
- **Render the human-facing status** with the canonical summary rather than hand-assembling it from raw fields:
  ```bash
  hpc-agent monitor-summary --run-id <run_id> --experiment-dir .
  ```
  Print the returned `headline` and `body` verbatim; `armed_hint` is the next-tick reminder while the run is in flight.

## `spec_invalid` from the skill

- `no_in_flight_run`: "No in-flight runs. Did you mean `/aggregate-hpc`?"
- `terminal_no_progress`: surface the failure pattern; the recovery (resubmit-from-scratch / investigate / abandon) is the user's nudge.

## For monitoring that outlives the chat

`/monitor-hpc` is one round-trip. To keep watching on a schedule:
- Schedule a recurring campaign-tick / status run in cron (the headless surface): when a brief's `monitor_arm.arm == "cron"`, pass `cron_create_args` to `CronCreate` verbatim — and when `arm == "none"` (terminal) or the run can no longer be resolved, `CronDelete` every cron naming that `run_id` (the skill's "Monitor-arm cron lifecycle" rule; a cron must never outlive its run).
- `/loop <interval> /monitor-hpc` — repeats the slash on an interval in-session.
- The `status-watch` block is detach-by-contract: it spawns a durable worker that survives a chat-session death and keeps polling to terminal. A dead worker is re-spawned by the next cron tick (a dead lease self-heals — never re-dialed inline) or surfaced by the doctor dead-worker scan, which DETECTS and drafts a recovery proposal but never restarts anything.
