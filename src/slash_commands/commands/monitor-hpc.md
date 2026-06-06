`/monitor-hpc` is the **human-interview wrapper** around the `hpc-status` skill — the agent-autonomous decision layer that polls an in-flight HPC run and decides whether to wait, resubmit failed tasks, or surface for investigation.

The slash conducts user-facing dialogs **after** the skill identifies what needs resolving.

## Execution style

- **Batch independent tool calls** into one parallel message — multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should not run serially.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.

## Invocation

Invoke the `hpc-status` skill via the Skill tool with the initial spec:

```
Skill("hpc-status", {
  experiment_dir: ".",
  run_id: <if user stated>,
  wait_terminal: <true if user requested wait, else false>
})
```

If `wait_terminal` is unset, default to `false` (snapshot) unless the user said "wait until done."

## Parallel startup

Monitoring is a long poll — don't leave the user idle until the first tick. **Dispatch the `hpc-status` skill in the background** (Claude Code's `Agent` tool `run_in_background: true`) and, in parallel, do the work that needs no fresh cluster output (#286):

- **Surface the journal snapshot first.** The run's last recorded `last_status` + lifecycle are on disk — render them immediately with `monitor-summary` so the user sees where the run stood without waiting for the poll.
- **Canvass the predictable decision.** When the local snapshot already shows a high failure count, ask `high_failure_rate_action` (investigate / resubmit / abandon) in parallel, so the verdict is ready when the poll confirms terminal.

Await the background poll at the join — immediate on the fast path (run already terminal). The user's answer folds into the surfaced result; if it conflicts with what the poll found (e.g. the run actually completed clean), drop it. Same shape as `/submit-hpc`'s parallel startup, ported per #286.

## On `needs_resolution` — walking ambiguities

### Dialog: `run_id`

Multiple in-flight runs. Show the candidates from the envelope, grouped by `campaign_id` when ≥3:

```
Multiple in-flight runs:
  1. <run_id> — submitted 2h ago, ml_ridge on hoffman2, 47/100 complete
  2. <run_id> — submitted 30m ago, dl_patchts on discovery, 0/24 complete
Which run?
```

### Dialog: `high_failure_rate_action`

```
Run <id> finished with N of M tasks failed (>10%). Options:
  [1] investigate (default) — inspect the failure pattern; don't auto-resubmit
  [2] resubmit — re-submit just the failed tasks
  [3] abandon — mark terminal; move on
Which?
```

The default is `investigate` because >10% failure usually means a real bug, and auto-resubmitting wastes more cluster time.

## On final envelope

Render the user-facing status with the canonical `monitor-summary` primitive — do **not** hand-assemble the framing from raw `lifecycle_state` / `complete_count` fields. Hand-framing reintroduces per-tick wording drift, which is exactly what this primitive removes (it reads the journal + the latest `.monitor.jsonl` tick and returns byte-stable `headline` + `body`):

```bash
hpc-agent monitor-summary --run-id <run_id> --experiment-dir .
```

Print the returned `headline` and `body` **verbatim**. `armed_hint` is non-null only while the run is still in flight — it's the one-line reminder to schedule the next monitor tick.

Then surface the decision-grade detail the summary doesn't carry:
- `data.report.decisions` — especially auto-resubmit decisions
- `data.report.anomalies`
- `data.report.result.failed_task_ids` when non-empty

## On `spec_invalid` (not `needs_resolution`)

- `no_in_flight_run`: "No in-flight runs. Did you mean `/aggregate-hpc`?"
- `terminal_no_progress`: surface the failure pattern; ask user whether to resubmit-from-scratch, investigate, or abandon.

## For monitoring that outlives the chat

`/monitor-hpc` is one round-trip per invocation. To poll on a schedule:

- Schedule a recurring campaign driver in cron (the driver's CLI is the headless surface).
- `/loop <interval> /monitor-hpc` — repeats the slash on an interval inside the chat session.
- External agent: invoke `Skill("hpc-status", {..., wait_terminal: true})` for a blocking poll — the skill's worker handles the loop in private context.
