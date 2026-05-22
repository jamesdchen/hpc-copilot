Do not run the `hpc-status` skill in this conversation's context. Delegate it to a fresh-context **subagent** to execute it (`skills/hpc-status/SKILL.md`) — the workflow is: poll-run-status vs monitor-flow choice, lifecycle dispatch, polling cadence, resubmit decision flow. The skill is the canonical SoT.

You do **not** hand-write the worker's prompt — `hpc-agent run` generates it deterministically. The flow:

1. Resolve the run to monitor with the human-facing resume-offer dialog below, in this conversation.
2. Run, via the `Bash` tool: `hpc-agent run status --fields-json '<fields>'`, where `<fields>` is a JSON object of the resolved inputs (`run_id`, intent: snapshot vs wait-until-terminal). It validates the fields, renders the canonical worker prompt, spawns a fresh-context worker that runs the `hpc-status` skill, and returns its report. You author only the `fields` data — never the prompt prose.
3. `hpc-agent run` prints a JSON envelope on stdout: `data.report` carries `result` (the skill's result envelope), `decisions` (the workflow's decision points and what the worker chose at each), and `anomalies`; `data.worker_exit_code` is the worker's exit status.
4. Surface `data.report.result` (`lifecycle_state`, `complete`/`total`, `failed_task_ids`, `escalation_reason`), the `decisions` list, and the `anomalies` string to the user. The verbose intermediate output — per-tick polls, SSH dumps, failed-task stderr tails — stayed in the worker.

This slash command is the human-facing entry point: the content below is the main agent's job — collect it here and pass it in `--fields-json`, do not delegate it. It carries one piece the skill cannot: the resume-offer dialog for cold-session recovery.

## Scheduling the next tick

Each `/monitor-hpc` invocation is **one tick**. A tick that runs inside the chat needs no follow-up — when it finishes, it finishes.

For monitoring that must outlive the chat session, the user schedules a recurring job that re-checks the run:

- **Cron** running the headless `hpc-campaign-driver --experiment-dir <dir>` — each tick is a fresh process, no exit contract. Use [decide-monitor-arm](../../docs/primitives/decide-monitor-arm.md) to pick a sensible cron cadence from the run's current state.
- **`/loop <interval> /monitor-hpc <args>`** when the user wants Claude Code to drive the cadence within a session.

Once the run reaches a terminal state (`complete` / `failed` / `abandoned`), cancel any cron that was scheduled for its run_id.

## Cold-session resume

1. **Resolve experiment dir**: `experiment_dir = cwd`.

2. **Check the run journal first**: invoke [list-in-flight](../../docs/primitives/list-in-flight.md). If `$ARGUMENTS` is empty AND in-flight is non-empty, present a one-line resume offer per candidate (most recent first):

   > "Found in-flight run: {profile} on {cluster}, jobs {job_ids}, last status {complete}/{total} complete @ {age(checked_at)} ago, waves combined {combined_waves}. Resume? [Y/n]"

3. **Group by `campaign_id` when displaying multiple in-flight runs.** Each `RunRecord` carries a `campaign_id` field; empty string for open-loop submits. When more than ~3 runs are in flight and at least one carries a campaign tag, render the offer grouped:

   > "Found 5 in-flight runs across 2 campaigns + 1 standalone:
   >  • campaign `ml_ridge_q1` (3 iterations in flight; last completed iteration's `loss=0.42`); resume with `/campaign-hpc status --campaign-id ml_ridge_q1` for the full history.
   >  • campaign `walk_forward_2026q1` (1 iteration in flight).
   >  • standalone run `<run_id>` ({profile} on {cluster}, last status {complete}/{total} @ {age} ago); resume with `/monitor-hpc --run-id <run_id>`.
   > Pick one, or skip to start fresh?"

   The flat per-run offer is fine for ≤3 in-flight; the campaign grouping kicks in for the long-running tuning / sweep cases where many runs may be active.

4. **Resolved run_id** → hand off to the **hpc-status** skill with the chosen run_id. The skill picks the snapshot vs wait-until-terminal surface based on the caller's intent (driven by chat context).

5. **Before exiting**, check `lifecycle_state` from the skill's response. If still in flight and the monitoring must outlive the chat, schedule the next tick per "Scheduling the next tick" above; if terminal, cancel any cron for the run_id.

## Notes

- **Resume offer is human UX.** The agent uses `list-in-flight` directly (no human prompt); the slash command is what shows the offer.
- **Cron arming reasoning** is per-run: pick interval based on the run's expected duration. Short jobs (< 1h) use 5min; medium (1-4h) use 15min; long (> 4h) use 30min. Long-running monitor cadence ramps up with the run age — see the skill for the internal cadence rules `monitor-flow` applies.
