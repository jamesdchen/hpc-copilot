`/monitor-hpc` is the **human-interview wrapper** around the `hpc-status` skill — the agent-autonomous decision layer that polls an in-flight HPC run and decides whether to wait, resubmit failed tasks, or surface for investigation.

This slash command's job is purely human-elicitation: when the user types `/monitor-hpc`, walk any disambiguation dialog (which run? wait until terminal?), then invoke the `hpc-status` skill with the resolved fields. The decision logic — polling cadence, resubmit thresholds, lifecycle dispatch — lives in the skill.

## Interview

### Run id

If `$ARGUMENTS` includes a `run_id`, use it. Otherwise, load context:

```bash
hpc-agent load-context --experiment-dir .
```

Read `data.in_flight`:

| Count | Dialog |
|---|---|
| 0 | "No in-flight runs. Did you mean to check a terminal run with `/aggregate-hpc`?" |
| 1 | Use it; skip the dialog. |
| 2+ | "Multiple in-flight runs:<br>1. `<run_id>` — submitted 2h ago, ml_ridge on hoffman2, 47/100 complete<br>2. `<run_id>` — submitted 30m ago, dl_patchts on discovery, 0/24 complete<br>Which run?" |

Group multi-run displays by `campaign_id` when ≥3 runs exist — easier to scan than a flat list.

### Wait mode

```
One-shot snapshot, or wait until the run reaches a terminal state?
  [1] snapshot (default) — print current state, return
  [2] wait — block until terminal; cadence 60s
Which?
```

## Handoff

Invoke the `hpc-status` skill via the Skill tool:

```
Skill("hpc-status", {
  experiment_dir: ".",
  run_id: "<resolved>",
  wait_terminal: <true|false>,
  mode: "interview"
})
```

The skill polls (one-shot or blocking per `wait_terminal`), interprets the lifecycle state, and applies the resubmit policy if appropriate. Returns the envelope.

Surface to the user:
- `data.report.result.lifecycle_state` (running / pending / complete / terminal_with_failures / terminal_no_progress)
- `data.report.result.complete_count`, `failed_task_ids`
- `data.report.decisions` — especially any auto-resubmit decisions the skill made
- `data.report.anomalies` — preemption, NODE_FAIL, scratch full, etc.

## On `spec_invalid` from the skill

| Error code | What to do |
|---|---|
| `ambiguous_run` | Show the candidates from the envelope; user picks. |
| `no_in_flight_run` | "No in-flight runs to monitor. Did you mean `/aggregate-hpc`?" |
| `high_failure_rate` | Surface the failure count + sample errors; ask user whether to investigate (default) or force-resubmit. |
| `terminal_no_progress` | Surface the failure pattern; user decides whether to resubmit-from-scratch, investigate, or abandon. |

## For monitoring that outlives the chat

`/monitor-hpc` is one round-trip per invocation. To poll on a schedule:

- Schedule a recurring campaign driver in cron (the driver's CLI is the campaign workflow's headless surface).
- `/loop <interval> /monitor-hpc` — repeats the slash on an interval inside the chat session.
- External agent (MARs experiment-runner, etc.): invoke `Skill("hpc-status", {..., wait_terminal: true, mode: "autonomous"})` for a blocking poll.
