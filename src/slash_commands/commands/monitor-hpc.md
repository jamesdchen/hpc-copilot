Monitor running HPC jobs via SSH and take corrective action.

Per-operation contracts live in `docs/primitives/` — this skill composes the [poll-run-status](../../docs/primitives/poll-run-status.md), [combine-wave](../../docs/primitives/combine-wave.md), [resubmit-failed](../../docs/primitives/resubmit-failed.md), [reconcile-journal](../../docs/primitives/reconcile-journal.md), and [mark-run-terminal](../../docs/primitives/mark-run-terminal.md) primitives behind a tick-driven adaptive monitoring loop. For envelope/exit-code shapes see `docs/reference/cli-spec.md`.

> ## ⚠️ EXIT CONTRACT — read before anything else
>
> Every `/monitor-hpc` invocation is **one tick that arms the next tick**, not a one-off. Before exiting, you MUST do exactly one of:
>
> 1. **Arm `CronCreate`** for any tick that may outlive the chat session is open (which for HPC monitoring is essentially always). Survives turn boundaries within the session; dies when the session ends.
> 2. **`/loop <interval> /monitor-hpc <args>`** when the user wants to drive the cadence themselves.
> 3. **Skip arming** only when the run reached a terminal state (`complete` / `failed` / `abandoned`) — and in that case you MUST cancel any existing cron for this run_id.
>
> Then emit the final line of stdout in this exact form:
>
>     armed: <cron|loop|none> run_id=<X> cadence=<Y>s reason="<short>"
>
> `none` is only valid when terminal-state cleanup ran. Anything else (including silent exit) is a spec violation. If you are about to exit without this line, you have not completed the tick — restart Step 5.
>
> A Stop hook in `~/.claude/settings.json` (installed via `hpc-agent hook-install`) verifies this line and blocks termination if missing.

## Setup

1. **Resolve experiment dir**: `experiment_dir = cwd`.

2. **Check the run journal first** (cold-session resume path): invoke the [list-in-flight](../../docs/primitives/list-in-flight.md) primitive (`session.find_in_flight_runs(Path.cwd())` is the Python entry point).

   If `$ARGUMENTS` is empty AND in-flight is non-empty, present a one-line
   resume offer per candidate (most recent first):

   > "Found in-flight run: {profile} on {cluster}, jobs {job_ids}, last
   > status {complete}/{total} complete @ {age(checked_at)} ago, waves
   > combined {combined_waves}. Resume? [Y/n]"

   **Group by `campaign_id` when displaying multiple in-flight runs.**
   Each `RunRecord` carries a `campaign_id` field; empty string for
   open-loop submits. When more than ~3 runs are in flight and at least
   one carries a campaign tag, render the offer grouped:

   > "Found 5 in-flight runs across 2 campaigns + 1 standalone:
   >  • campaign `ml_ridge_q1` (3 iterations in flight; last completed
   >    iteration's `loss=0.42`); resume with `/campaign-hpc status
   >    --campaign-id ml_ridge_q1` for the full history.
   >  • campaign `walk_forward_2026q1` (1 iteration in flight).
   >  • standalone run `<run_id>` ({profile} on {cluster}, last status
   >    {complete}/{total} @ {age} ago); resume with `/monitor-hpc --run-id
   >    <run_id>`.
   > Pick one, or skip to start fresh?"

   The flat per-run offer is fine for ≤3 in-flight; the campaign
   grouping kicks in for the long-running tuning / sweep cases where
   the flat list would be noisy. `slash_commands.session.find_runs_by_campaign(cwd, cid)`
   gives you the per-campaign record list when you need it.

   On `Y` (default on empty), hydrate `cluster`, `ssh_target`, `remote_path`,
   `job_name`, `job_ids`, `run_id`, `combined_waves`, `failed_waves`,
   `retries` from the run record. Skip the cluster prompt below. The
   per-run sidecar at `.hpc/runs/<run_id>.json` carries cmd_sha,
   executor, result_dir_template, and wave_map; `.hpc/tasks.py` carries
   the per-task kwargs.

   Then invoke the [reconcile-journal](../../docs/primitives/reconcile-journal.md) primitive BEFORE Step 0.5 — re-derives ground truth from the cluster and writes it back. If the primitive returns `lifecycle_state == "abandoned"` (zero recorded job_ids known to the scheduler), prompt the user: "Recorded jobs no longer exist. Mark abandoned, or start fresh?" — never silently mutate scheduler state.

   On `n`, fall through to step 3 below.

3. **No journal hit — fall back to existing context sources** (priority order):

   - If `$ARGUMENTS` contains `--cluster <name>`, use that cluster.
   - Else read `cluster` from the most recent matching `.hpc/runs/<run_id>.json` sidecar.
   - Else check Claude Code memory for cached cluster preference.
   - Else ask the user.

4. Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from cluster config + cached/configured remote path.

5. Load the run's identity and task definition. Two files together describe the run:

   - `.hpc/runs/<run_id>.json` — the per-run sidecar: cmd_sha, executor command, `result_dir_template`, task_count, wave_map, claude_hpc_version, submitted_at.
   - `.hpc/tasks.py` — the user's `total()` / `resolve(task_id)` module. Per-task kwargs come from `tasks.resolve(i)`; per-task `result_dir` is the sidecar's `result_dir_template.format(task_id=i, run_id=<run_id>, **kwargs)`.

   ```python
   from claude_hpc import load_tasks_module, read_run_sidecar, tasks_path
   sidecar = read_run_sidecar(experiment_dir, run_id)
   tasks = load_tasks_module(tasks_path(experiment_dir))
   ```

   Recovery order if the local sidecar is missing:

   1. Pull from the cluster:
      ```bash
      mkdir -p .hpc/runs
      rsync -az $SSH_TARGET:$REMOTE_PATH/.hpc/runs/<run_id>.json ./.hpc/runs/<run_id>.json
      ```
   2. `.hpc/tasks.py` should already be in your local repo (it's git-tracked). If it's missing, the experiment was never properly scaffolded — re-run `/submit-hpc` and follow Step 6's scaffolding flow.
   3. If both are missing on the cluster too, fall back to reading `logs/<job_name>_<job_id>_<task_id>.out` for each task as the last-resort artifact and surface the gap to the user — monitoring can only be partial without the sidecar.

## SSH Quoting

Single-quote the remote command so variables expand on the cluster, not locally:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && echo $SGE_TASK_ID'
```

## Step 0: Understand the Executor

Run `python -m <executor_module> --help` (extract the module from the profile's `run` command) to understand the executor's CLI interface. When diagnosing failures:

1. Read the traceback from the error log to find the failing file and line
2. Read that file directly
3. If you need upstream context, follow imports from the failing file
4. Use Glob/Grep to find related files if needed

## Operating Principles

1. **Sanity-check before and after.** Run Step 0.5 pre-flight before scheduling the next tick; run Step 4a post-flight before aggregating in Step 4b. File counts lie and stale job IDs waste hours — never skip these gates.
2. **Act autonomously on known failures.** For OOM, walltime, and node failures, immediately resubmit with appropriate resource overrides. Do NOT ask for permission. Only pause for code bugs or unrecognized errors.
3. **Adaptive re-invocation, not streaming.** After each tick, schedule exactly one follow-up via **`CronCreate`** (the only programmatic mechanism with a documented public API in Claude Code; ``ScheduleWakeup`` is internal/undocumented and must not be used). Pick the cadence from Step 5's adaptive table. The user can also drive ticks themselves via `/loop <interval> /monitor-hpc <args>`. Do **not** arm a persistent `Monitor` subprocess — it wastes the 5-min prompt cache and burns an idle process. State is recovered from the run journal at each tick.
4. **Silent-by-default tick output.** Each tick writes a structured record to `.hpc/runs/<run_id>.monitor.jsonl` (the **tick log**, see next section) and produces **no console output** unless an action was taken (auto-resubmit), a terminal state was reached (`complete` / `failed` / `abandoned`), or the user must intervene (code bug, unknown failure, second-strike combiner failure). All status / rollup / pre-flight / post-flight observations go to the JSONL — they are never narrated to the conversation. Token cost adds up across hour-scale monitoring; the silent-by-default policy keeps it bounded. When the user comes back and asks "what happened" / "status" / "summarize", switch into **Summary mode** (Step 7) which reads the JSONL and produces a single digest.

## Tick log

Path: `.hpc/runs/<run_id>.monitor.jsonl` (newline-delimited JSON, one record per tick, append-only).

Per-record schema (all fields required unless marked optional):

```json
{
  "tick_id": "2026-05-02T20:50:12Z",
  "run_id": "ml_xgboost-20260502-204000-a1b2c3d4",
  "summary": {"complete": 47, "running": 3, "pending": 0, "failed": 0, "unknown": 0},
  "diff_from_prev": {"newly_complete": [42, 43], "newly_failed": [], "newly_combined_waves": [4]},
  "preflight": "ok",
  "actions": [
    {"kind": "resubmit", "task_ids": [42], "category": "system_oom", "overrides": {"mem": "8G"}, "new_job_ids": ["7591234.1"]}
  ],
  "lifecycle_state": "in_flight",
  "next_tick_seconds": 270,
  "console_emitted": false
}
```

Append-only via small Python snippet (do NOT use `>>` from a shell — concurrent ticks would interleave bytes; the per-run sidecar lock in `runner` already serializes monitor invocations on the same run_id). Every step that today says "report to user" or "format ... into the user-facing table" instead writes its observations into the next tick record and emits nothing to the console.

The first tick on a fresh run creates the file; subsequent ticks read the **last record** (the previous tick's snapshot) for the diff computation in Step 1, then append their own. There is no compaction, no rotation — at typical 5-minute monitoring cadences a 24-hour run produces ~290 lines (~80KB), well below any reasonable limit.

## Arguments

$ARGUMENTS formats (pick one):

1. **Job name + monitor** (no job-ids — checks active jobs by name):
   `<job_name>` or `<job_name> --cluster <name>`

2. **Monitor existing** (job-ids provided):
   `<job_name> <job_ids> [total_tasks]`
   Example: `ml_ridge 12345678 100`

3. **Auto-discover** (empty):
   Check for active jobs belonging to the current project via queue status commands. Read `.hpc/runs/` to identify recent runs (newest-first via `find_existing_runs`) and pick the one matching the active job IDs. Each sidecar carries the run's `task_count`, `executor`, and `wave_map`.

### Optional: `--tui`

Append `--tui` to any of the above formats to launch a live terminal dashboard
instead of running the one-shot JSON poll. This is for **interactive, attended**
monitoring only — the default cron / self-scheduling path stays JSON. Do not
schedule a TUI session via `CronCreate`.

The TUI reuses the same `report_status` code path (with the run_id-keyed
sidecar adapter on the cluster), so the JSON contract described below
is unchanged. Rich is an optional dep (`pip install 'claude-hpc[tui]'`);
if it's missing, `--tui` prints a short install hint and exits 2, and
plain `/monitor-hpc` continues to work.

Keybinds:

| Key | Action |
|-----|--------|
| `r` | Force an immediate refresh (skips the poll interval) |
| `f` | Toggle focus on the failing-tasks panel |
| `l` | Open the currently focused task's error log via `ssh <host> less <log>` |
| `q` | Quit the TUI |

Invoke the module directly:

```bash
python -m claude_hpc.mapreduce.reduce.tui \
    --run-id <run_id> \
    --job-ids <csv_job_ids> \
    --job-name <profile> \
    --log-dir logs \
    --poll-interval 30 \
    --ssh-target <user>@<host>
```

The TUI surfaces four panels: header (run_id / cluster / scheduler /
wall-clock), per-grid-point rollup (queued / running / done / failed),
wave progress bars sourced from the sidecar's `wave_map`, failure
classification counts (via `classify_failure`), plus a tail of the 10
most recent failing tasks with one-line diagnostics. The footer shows
live CPU-hour / GPU-hour totals from `resource_usage`.

## Step 0.5: Pre-flight Sanity Checks

Run **once** per `/monitor-hpc` invocation, immediately after Step 0 and **before** Step 1. If any check fails, write `preflight: "failed:<which check>"` into the tick record, **emit one console line** `pre-flight FAILED: <which check>`, and stop — do NOT proceed to Step 1 and do NOT schedule a follow-up tick in Step 5.

On success, write `preflight: "ok"` into the tick record. **Emit nothing to console.**

### 0.5a — Job IDs are real

Every job ID the user passed must be known to the scheduler. Stale IDs from a prior run lead to nonsense status reports.

```bash
# SLURM
ssh $SSH_TARGET 'squeue -j '"$JOB_IDS"' -h -o "%i %T" 2>&1; sacct -j '"$JOB_IDS"' -n -P -o JobID,State 2>&1 | head'

# SGE
ssh $SSH_TARGET 'qstat -j '"$JOB_IDS"' 2>&1 | head'
```

A job that is neither in the queue nor in the accounting DB → abort.

### 0.5b — Sidecar + tasks.py are consistent

```bash
mkdir -p .hpc/runs
rsync -az $SSH_TARGET:$REMOTE_PATH/.hpc/runs/<run_id>.json ./.hpc/runs/<run_id>.json
python -c '
import json
from claude_hpc import load_tasks_module, tasks_path
sc = json.load(open(".hpc/runs/<run_id>.json"))
print(sc["sidecar_schema_version"], sc["task_count"])
print(load_tasks_module(tasks_path(".")).total())'
```

Verify `sidecar_schema_version >= 1` and `task_count` matches both `tasks.total()` and the value the user passed (or the sum of array sizes from squeue/qstat). A mismatch suggests the local `.hpc/tasks.py` was edited after submission; re-run `/submit-hpc` to write a fresh sidecar with the current task list.

### 0.5c — Executor imports cleanly

Catches Python import errors that would otherwise produce 100 identical task failures before anyone notices.

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && python -m '"$EXECUTOR_MODULE"' --help 2>&1 | tail -20'
```

Non-zero exit, `ImportError`, or `ModuleNotFoundError` → abort. The executor module was extracted from the profile's `run` command in Step 0.

### 0.5d — Result-dir parent is writable

Spot-check one task's `result_dir` parent. Compute the path from the
sidecar's `result_dir_template` against `tasks.resolve(0)`:

```python
ctx = {"task_id": 0, "run_id": run_id, **tasks.resolve(0)}
result_dir = sidecar["result_dir_template"].format(**ctx)
```

```bash
ssh $SSH_TARGET 'parent=$(dirname '"$RESULT_DIR"'); [ -d "$parent" ] && [ -w "$parent" ] && echo OK || echo "NOT_WRITABLE: $parent"'
```

Anything other than `OK` → abort.

## Step 1: Poll + auto-combine via `monitor-flow`

> **Anti-pattern (critical):** Do NOT use `ls logs/ | wc -l` or `find results/ -name '*.csv' | wc -l` as a success proxy. **Failed tasks produce logs and partial output too** — an `ImportError` still leaves an `.o*` file; a mid-run crash still leaves a `_wip_*` directory. Counting these as "progress" has led to hours of wasted cluster time discovering 100% failure rates after submission. The `monitor-flow` workflow atom invoked below uses the deterministic cluster-side status reporter; trust its envelope, not file counts.

The slash command's tick body is **one `monitor-flow` invocation**. The atom does the poll (Step 1's old job), the wave combination (the old Step 1c), the tick log write (the old Step 6), and returns when terminal OR when its budget elapses. The slash command branches on the returned `lifecycle_state`.

```bash
hpc-agent monitor-flow --spec .hpc/runs/<run_id>.monitor.spec.json --experiment-dir .
```

Spec shape (matches `schemas/monitor_flow.input.json`):

```json
{
  "run_id": "<run_id>",
  "poll_interval_seconds": 60,
  "wall_clock_budget_seconds": <adaptive — see Step 5 table>,
  "auto_combine_waves": true,
  "combiner_max_retries": 1,
  "file_glob": "<results.summary_pattern or *>"
}
```

Set `wall_clock_budget_seconds` to the adaptive delay from Step 5's table (60s–3600s). The atom polls at `poll_interval_seconds` until terminal or budget; each poll appends one record to `.hpc/runs/<run_id>.monitor.jsonl` (the same tick log that summary mode reads).

Branch on `data.lifecycle_state`:

| `lifecycle_state` | Meaning | Action |
|---|---|---|
| `complete` | Every task reported complete; `mark_terminal(complete)` was called | Go to Step 4a (post-flight verification) |
| `failed` | Failures with no work left; `escalation_reason` set | Go to Step 2 (diagnose) |
| `abandoned` | Recorded jobs no longer on scheduler | Re-invoke `reconcile-journal`; surface to user |
| `timeout` | Budget elapsed; cluster jobs still running | Go to Step 5 (schedule next tick) |

`data.failed_waves` non-empty (with any lifecycle) means at least one combiner hit `combiner_max_retries` — surface one console line `combine_wave: max retries on waves <list>, see .hpc/runs/<run_id>.monitor.jsonl` and continue.

### Step 1b: Stall detection (advisory; not in the atom)

If three consecutive `monitor-flow` ticks return `lifecycle_state: "timeout"` with the same `last_status` counts AND all tasks are pending/queued, that's a stall. Treat as Step 2 category `queue_stall`. (The atom doesn't classify stalls — that's judgment that lives in the slash command.)

## Step 2: Diagnose Failures

Read error logs for failed tasks. Log files follow the naming convention `{job_name}_{job_id}_{task_id}.{out|err}` where `job_name` is the profile name (the value passed to `--job-name` during submission). For example, profile `patchts` with job ID `7580680` produces `logs/patchts_7580680_1.out` and `logs/patchts_7580680_1.err`.

Read **both** `.out` and `.err` for each failed task (tail -50 each):
```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && tail -50 logs/<job_name>_<job_id>_<task_id>.err && tail -50 logs/<job_name>_<job_id>_<task_id>.out'
```

If the expected files don't exist, fall back to globbing:
```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/logs/*<job_id>_<task_id>.err 2>/dev/null'
```

Check job accounting (qacct for SGE, sacct for SLURM).

Classify the failure:

| Pattern | Category | Action |
|---------|----------|--------|
| `CUDA out of memory` / `OutOfMemoryError` | GPU OOM | Resubmit with more memory + smaller batch |
| High memory usage + exit !=0 | System OOM | Resubmit with higher memory limit |
| Time limit exceeded | Walltime | Resubmit with longer walltime |
| Node failure / `Eqw` / `NODE_FAIL` | Infra issue | Resubmit as-is on a different node (`--exclude=<failed_node>`) |
| `exit -11` / SIGSEGV / no traceback | Node SEGV | **STOP. Surface the SEGV node to the user.** A retry is not auto-safe |
| All tasks pending >15min / unchanged across 2 checks | Queue stall | Delete stalled job, resubmit with GPU fallback |
| Python traceback with clear bug | Code bug | **STOP. Report to user. Do NOT resubmit.** |
| Unrecognized error | Unknown | **STOP. Read full log, report to user.** |

**AUTONOMY RULE**: For OOM, walltime, node failures, and queue stalls — act immediately. Only STOP for code bugs, SEGVs, and unrecognized errors.

### On SEGV

A SIGSEGV without a Python traceback is the strongest "node may be silently degraded" signal. There is no auto-blacklist anymore — surface the failed node + the canary's stderr tail to the user instead. The user decides whether to retry, fix the executor, or use `--exclude=<node>` on a manual resubmit.

## Step 3: Resubmit Failed Tasks

Check retry count. The profile's `max_retries` (default 3) is the limit. If exceeded, report to user and skip.

### Resource overrides by failure type

| Failure | Retry 1 | Retry 2+ |
|---------|---------|----------|
| GPU OOM | 2x memory, batch_size/2 | 4x memory, batch_size/4 |
| System OOM | 2x memory | 4x memory |
| Timeout | 2.5x walltime | 3.5x walltime |
| Node fail | no overrides | no overrides |
| Queue stall | switch GPU type (use `gpu_fallback` from profile, or `gpu_types` from cluster) | next GPU in fallback |

Build the resubmission command using the same dispatch mechanism. The task IDs in the resubmission correspond to the same `.hpc/tasks.py` indices (resolved at task time on the cluster); the existing per-run sidecar at `.hpc/runs/<run_id>.json` is reused unchanged. Apply resource overrides to the submission flags.

**Update your job-ids list** for subsequent status checks.

After the resubmission backend call returns the new job IDs, invoke the [resubmit-failed](../../docs/primitives/resubmit-failed.md) primitive with the failed task IDs, the failure category from `classify_failure`, the resource overrides applied, and the new job IDs. The primitive increments `retries[tid].attempts` and records category + overrides; a future cold session sees the retry count and won't blow past `max_retries`.

**Console output for an auto-resubmit: exactly one line.** Example: `[monitor] resubmit task_ids=[42,51] category=system_oom overrides={mem:8G} new_jobs=[7591234.1]`. Set `console_emitted: true` in the tick record so summary mode knows the user already saw something for this tick. For escalations (code bug, unknown error, max_retries exceeded), emit a multi-line report with the traceback excerpt and explicit "needs your input" — these are the only times tick-time console output is allowed to be more than one line.

## Step 4a: Post-flight Verification

Run **once** when Step 1 reports `all_complete`, **before** any aggregation in Step 4b. If any check fails, emit `post-flight FAILED: <which check>` and stop — do NOT invoke `results.aggregate_cmd`, do NOT auto-claim completion. The job may have produced files but not real results.

This guards against the "file count lies" failure mode (see anti-pattern at Step 1) — `summary.complete == total_tasks` from the status reporter is a necessary but not sufficient condition for success.

### 4a.1 — Non-empty rows

Re-invoke the [poll-run-status](../../docs/primitives/poll-run-status.md) primitive's underlying cluster-side reporter with `--min-rows N` (a flag of the on-cluster `python -m claude_hpc.mapreduce.reduce.status` script that the primitive wraps; see `docs/reference/python-api-contract.md` for the cluster-side script's args). `N` is a profile-appropriate floor (1 minimum, more if the profile knows the expected row count). Any task that previously read `complete` but flips to `failed` here had an empty/short result file. Report which task IDs failed.

### 4a.2 — Spot-check 3 tasks

Pick the first, middle, and last task IDs (`0`, `task_count // 2`, `task_count - 1`). For each, read the head of its result file and verify:

- The file exists and is non-empty.
- Expected columns are present (use `results.summary_pattern` and the executor's known schema).
- Key metric column has at least one non-NaN value.

The per-task `result_dir` is computed locally from the sidecar:

```python
ctx = {"task_id": tid, "run_id": run_id, **tasks.resolve(tid)}
result_dir = sidecar["result_dir_template"].format(**ctx)
```

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && for tid in '"$FIRST $MID $LAST"'; do
  rd=<formatted result_dir for this tid>
  echo "=== tid=$tid rd=$rd ==="
  ls -la "$rd" 2>&1 | head -5
  for f in "$rd"/'"$SUMMARY_PATTERN"'; do head -3 "$f"; done
done'
```

### 4a.3 — File count cross-check

Confirm the count of result files on disk equals `summary.complete` from the status reporter. If they disagree, a task is reporting `complete` without an artifact (or vice versa) — abort and surface the count.

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && find results/ -name "'"$SUMMARY_PATTERN"'" 2>/dev/null | wc -l'
```

### 4a.4 — Late-stage error grep

Some tasks exit 0 after printing an error (cleanup ran successfully). Catch them.

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && grep -lE "Traceback|Error|Killed|OOM|assert" logs/'"$NAME"'_'"$JOB_ID"'_*.{out,err} 2>/dev/null | head'
```

Any hits → report the task IDs to the user before aggregating; let them decide whether to proceed or re-run.

If all four checks pass → emit `post-flight: OK` and continue to Step 4b.

## Step 4b: Aggregate (if configured)

If all waves have been combined (check `combined_waves` against total waves in `wave_map`), use the fast path:
1. `rsync -az $SSH_TARGET:$REMOTE_PATH/_combiner/ ./_combiner/`
2. Use `reduce_partials("_combiner/")` to merge partials into final aggregated metrics
3. Skip the standard aggregation command

When all tasks complete and the profile has `results.aggregate_cmd`:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && <results.aggregate_cmd>'
```

After aggregation:
1. Verify output files exist using `results.summary_pattern`.
2. Download summaries locally via rsync (include only summary patterns, exclude everything else).
3. Read and report key findings from the local summary files.

### Multi-Stage Progression

If the current stage completes and another stage has `depends_on` pointing to this stage, check the `depends_on` graph for newly unblocked stages. For each unblocked stage, prompt: "Stage `<next_stage>` is now unblocked (depends on `<this_stage>`). Submit it? (`/submit-hpc <profile_name>.<next_stage>`)"

## Step 5: Schedule the Next Tick (route through `decide-monitor-arm`)

**Don't hand-author the arm decision or the `armed:` line.** Call the `decide-monitor-arm` primitive — it's the single source of truth for the adaptive table, the cron schedule string, and the literal `armed:` line the Stop hook checks for:

```bash
# Spec file: the run's current state.
cat > /tmp/arm_spec.json <<EOF
{
  "run_id": "$RUN_ID",
  "summary": $LAST_STATUS_SUMMARY,         # {complete, running, pending, failed}
  "total_tasks": $TOTAL_TASKS,
  "invocation_argv": "/monitor-hpc $ORIGINAL_ARGS",
  "user_invoked_via_loop": $USER_LOOP,     # true iff this tick is under /loop
  "eta_sec": $ETA_OR_NULL,                 # optional; from Step 1's pace estimate
  "pace_unstable": false,                  # optional
  "queue_wait_sec": null                   # optional; from queue-wait predictor
}
EOF
hpc-agent decide-monitor-arm --spec /tmp/arm_spec.json
```

The envelope's `data` carries:

* `arm` — `cron` / `loop` / `none`
* `cadence_sec`, `schedule`, `reason`
* **`armed_line`** — copy this VERBATIM as the very last line of your response. The Stop hook (installed via `hpc-agent hook-install`) blocks the turn if it's missing or doesn't match the regex. The primitive guarantees a matching line; hand-authoring is the failure mode this fix exists to eliminate.
* **`cron_create_args`** — when `arm == "cron"`, pass these three keys (`schedule`, `prompt`, `reason`) directly into `CronCreate`. No string formatting on your end.

**Two valid arm mechanisms** (the only ones backed by documented public Claude Code tools):

- **`CronCreate`** (default; `arm == "cron"`): pass `data.cron_create_args` verbatim. If a previous tick already created a cron for this run_id and the cadence changed, `CronUpdate` (or `CronDelete` + `CronCreate`) — same prompt so the cron is idempotent on the run.
- **`/loop`-driven** (`arm == "loop"`): the user is already driving the cadence; do NOT register a cron, just emit the `armed_line`.
- **Terminal** (`arm == "none"`): the run is complete / failed / abandoned / timeout. Cancel any prior cron via `CronDelete`, emit the `armed_line`, exit. The primitive sets `cadence_sec=0` and `cron_create_args=null` to make this unambiguous.

> **Do not call `ScheduleWakeup`.** It's an internal/undocumented Claude Code tool, not in the public tools reference, and not emitted by `decide-monitor-arm`.

Then exit. The next invocation re-enters from Setup, hydrates state from the run journal (`session.find_in_flight_runs`), and runs Step 0.5 → Step 1 again.

### Required final line — exit contract (still enforced)

The Stop hook checks for `armed: <cron|loop|none> run_id=<X> cadence=<Y>s reason="<short>"` as the last line of stdout. `decide-monitor-arm`'s `data.armed_line` is exactly this format — copy-paste, no edits. If you find yourself constructing the line by hand, you've drifted off the spec; restart Step 5 and use the primitive.

### Reacting on the next tick

The follow-up `/monitor-hpc` invocation re-runs Step 1 and **diffs against the last journal snapshot** (the previous tick's `summary` and `tasks` recorded by `runner.reconcile`). React based on the diff:

| Diff | Action |
|---|---|
| Newly-failed task IDs | Step 2 (diagnose) → Step 3 (resubmit if auto-handled) → Step 5 (reschedule with new job IDs). |
| Newly-complete wave | Step 1c (combiner) → Step 5 (reschedule, same job IDs). |
| `summary.complete == total` | Step 4a (post-flight) → Step 4b (aggregate) → Step 6 (report) → `runner.mark_terminal(..., status='complete')`. Do NOT reschedule. |
| `failed > 0` and `running == 0` and no auto-handled category | Step 2 (diagnose) → escalate to user. Leave run `in_flight` unless user abandons (`runner.mark_terminal(..., status='failed')`). Do NOT reschedule. |
| State unchanged AND all-pending AND ≥2 ticks since last change | Treat as `queue_stall` (Step 2). Do NOT reschedule blindly — diagnose first. |
| State unchanged otherwise | Reschedule one more tick at the adaptive delay. |

### When the follow-up fires

- **CronCreate**: fires in the same Claude Code session at the registered cadence as long as the app is open. Survives turn boundaries (user closing one chat and starting another within the same session, idle waits between turns). Dies when the user closes Claude Code; on `--resume` of an unexpired cron, fires resume. Setup hydrates from `session.find_in_flight_runs` and runs `runner.reconcile` before Step 0.5.
- **`/loop` user-driven**: Setup hydrates from `session.find_in_flight_runs` and runs `runner.reconcile` before Step 0.5; otherwise identical to a CronCreate-fired tick.

**Terminal-state cleanup**: when a tick observes `summary.complete == total` (or any other terminal state), it MUST cancel the cron for that run_id via `CronDelete` before exiting. Forgetting this leaves a dead cron firing forever against a finished run.

Either way the slash command is responsible for its own state — never assume a long-lived in-memory variable persists across ticks.

## Step 6: Tick record (handled by `monitor-flow`)

`monitor-flow` writes one JSONL record per internal poll to `.hpc/runs/<run_id>.monitor.jsonl` — the slash command no longer writes the tick log directly. After `monitor-flow` returns, the slash command may APPEND additional `actions` entries to the most recent record IF it took action this tick (e.g. an auto-resubmit from Step 3) by re-opening the JSONL and rewriting the last line. Otherwise the atom's record is the truth and the slash command writes nothing.

**Console output rule (load-bearing — this is what makes /monitor-hpc cheap to run on long jobs):**

| Condition | Console |
|---|---|
| `monitor-flow` returned `timeout`, no diff vs prior tick, no slash-command actions | **silent**; schedule next tick |
| `monitor-flow` returned `timeout`, diff present (waves combined, etc.) | **silent**; schedule next tick |
| Auto-resubmit fired (Step 3) | one line: `[monitor] resubmit ...` |
| Combiner 2nd failure (`failed_waves` non-empty after this tick) | one line + `STOP: needs user` |
| Code bug / unknown failure / max_retries exceeded | full report, multi-line |
| `monitor-flow` returned `complete` / `failed` / `abandoned` | one line: `[monitor] <state> run_id=<id> — /monitor-hpc <run_id> summary for digest` |
| Pre-flight failed (Step 0.5) | one line: `pre-flight FAILED: <which>` |

**The default is silent.** Token cost is the reason — at 5-minute monitoring on a 24-hour run, a single chatty tick × 290 ticks adds up fast.

## Step 7: Summary mode (route through `monitor-summary`)

Triggered by either:

- `$ARGUMENTS` ends in the literal token `summary` (e.g. `/monitor-hpc summary`, `/monitor-hpc <run_id> summary`), OR
- The user's natural-language message asks "what happened" / "how's it going" / "status" / "summarize" while there is at least one in-flight run with a tick log on disk.

In this mode, do **not** run Step 0.5 / Step 1 / SSH polling. Do NOT contact the cluster. **Don't hand-author the summary** — call the `monitor-summary` primitive, which reads `.hpc/runs/<run_id>.monitor.jsonl` + the run journal and returns the canonical user-facing digest:

```bash
hpc-agent monitor-summary --experiment-dir . --run-id <run_id>
```

The envelope's `data` carries `{lifecycle_state, headline, body, armed_hint}`. Print `headline` then `body` verbatim — that's the entire summary the user sees. The framing is byte-stable across ticks for the same input state, so consecutive summary requests don't drift in wording.

When `lifecycle_state` is terminal (`complete` / `failed` / `abandoned` / `timeout`), `armed_hint` is null — the slash command exits without re-arming. Otherwise, `armed_hint` reminds you to call `decide-monitor-arm` next; in summary mode the arm step is skipped (the user just wanted a digest), but the `armed:` exit-line is still required because the Stop hook runs unconditionally — call `decide-monitor-arm` and emit its `armed_line` after the summary body.

Summary mode is the **only** time `/monitor-hpc` is allowed to be verbose (one `monitor-summary` envelope's worth, no more). Don't repeat the digest across consecutive turns unless the user asks again.

## Context Management

1. **Each tick is independent.** One `/monitor-hpc` invocation = one tick (Setup → preflight → status → react → write tick record → schedule next → exit). State persists in the run journal + the tick log, not in the conversation.
2. **One status query per tick.** Pre-flight + Step 1 reporter run once. The agent does NOT loop internally; the next tick comes from `CronCreate` (default) or `/loop` (user-driven).
3. **Diffs go in the tick record, not the console.** Compare Step 1's `summary`/`tasks` against the prior tick's record (last line of the JSONL). Populate `diff_from_prev`. If state is fully unchanged the diff is empty arrays — that's fine; the JSONL grows but the console stays silent.
4. **Minimize tool output.** Use `tail -20` for logs. Prefer compact status commands over verbose output.
5. **If the session ends:** `CronCreate`-scheduled ticks die when Claude Code closes; on `--resume` of an unexpired cron they fire again. If the user reopens after a long absence, re-running `/monitor-hpc` (no args) hydrates the in-flight run from the journal at `~/.claude/hpc/<repo_hash>/`, runs `runner.reconcile`, and re-arms the cron. The journal always carries last-known status.
