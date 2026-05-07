Invoke the `hpc-status` skill via the Skill tool (`skills/hpc-status/SKILL.md`) for the workflow: poll-run-status vs monitor-flow choice, lifecycle dispatch, polling cadence, resubmit decision flow. The skill is the canonical SoT.

This slash command is the human-facing entry point. It carries two pieces of content the skill cannot: the **EXIT CONTRACT** (which is slash-specific because a Stop hook validates this command's stdout), and the resume-offer dialog for cold-session recovery.

## ⚠️ EXIT CONTRACT — read before anything else

Every `/monitor-hpc` invocation is **one tick that arms the next tick**, not a one-off. Before exiting, you MUST do exactly one of:

1. **Arm `CronCreate`** for any tick that may outlive the chat session is open (which for HPC monitoring is essentially always). Survives turn boundaries within the session; dies when the session ends.
2. **`/loop <interval> /monitor-hpc <args>`** when the user wants to drive the cadence themselves.
3. **Skip arming** only when the run reached a terminal state (`complete` / `failed` / `abandoned`) — and in that case you MUST cancel any existing cron for this run_id.

Then emit the final line of stdout in this exact form:

```
armed: <cron|loop|none> run_id=<X> cadence=<Y>s reason="<short>"
```

`none` is only valid when terminal-state cleanup ran. Anything else (including silent exit) is a spec violation. If you are about to exit without this line, you have not completed the tick — restart from the cold-session-resume step below.

A Stop hook in `~/.claude/settings.json` (installed via `hpc-agent hook-install`) verifies this line and blocks termination if missing.

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

5. **Before exiting**, check `lifecycle_state` from the skill's response and follow the EXIT CONTRACT above.

## Notes

- **The EXIT CONTRACT is the load-bearing slash-specific bit.** Without it, `/monitor-hpc` invocations silently fail to schedule the next tick, and runs sit unwatched until the user notices. The Stop hook validates the final stdout line; missing it blocks termination.
- **Resume offer is human UX.** The agent uses `list-in-flight` directly (no human prompt); the slash command is what shows the offer.
- **Cron arming reasoning** is per-run: pick interval based on the run's expected duration. Short jobs (< 1h) use 5min; medium (1-4h) use 15min; long (> 4h) use 30min. Long-running monitor cadence ramps up with the run age — see the skill for the internal cadence rules `monitor-flow` applies.
