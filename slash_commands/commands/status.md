Monitor running HPC jobs via SSH and take corrective action.

CLI shapes for every tool referenced below: see `docs/cli-contract.md`.

## Setup

1. **Resolve experiment dir**: `experiment_dir = cwd`.

2. **Check the run journal first** (cold-session resume path):

   ```python
   from pathlib import Path
   from slash_commands import session, runner

   in_flight = session.find_in_flight_runs(Path.cwd())
   ```

   If `$ARGUMENTS` is empty AND `in_flight` is non-empty, present a one-line
   resume offer per candidate (most recent first):

   > "Found in-flight run: {profile} on {cluster}, jobs {job_ids}, last
   > status {complete}/{total} complete @ {age(checked_at)} ago, waves
   > combined {combined_waves}. Resume? [Y/n]"

   On `Y` (default on empty), hydrate `cluster`, `ssh_target`, `remote_path`,
   `job_name`, `job_ids`, `run_id`, `combined_waves`, `failed_waves`,
   `retries` from the run record. Skip the cluster prompt below. The
   per-run sidecar at `.hpc/runs/<run_id>.json` carries cmd_sha,
   executor, result_dir_template, and wave_map; `.hpc/tasks.py` carries
   the per-task kwargs.

   Then call `runner.reconcile(Path.cwd(), run_id, scheduler=<scheduler>)`
   BEFORE Step 0.5. Reconcile re-derives ground truth from the cluster
   (fresh status report, canonical `combined_waves` from `_combiner/wave_*.json`,
   alive job-ID check) and writes it back to the journal in three parallel
   SSH calls. If `reconcile` flips status to `'abandoned'` (zero recorded
   job_ids known to the scheduler), prompt the user: "Recorded jobs no longer
   exist. Mark abandoned, or start fresh?" — never silently mutate scheduler
   state.

   On `n`, fall through to step 3 below.

3. **No journal hit — fall back to existing context sources** (priority order):

   - If `$ARGUMENTS` contains `--cluster <name>`, use that cluster.
   - Else if `hpc.yaml` exists, read `cluster` field.
   - Else check Claude Code memory for cached cluster preference.
   - Else ask the user.

4. Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from cluster config + cached/configured remote path.

5. Load the run's identity and task definition. Two files together carry what used to live in the manifest:

   - `.hpc/runs/<run_id>.json` — the per-run sidecar: cmd_sha, executor command, `result_dir_template`, task_count, wave_map, claude_hpc_version, submitted_at.
   - `.hpc/tasks.py` — the user's `total()` / `resolve(task_id)` module. Per-task kwargs come from `tasks.resolve(i)`; per-task `result_dir` is the sidecar's `result_dir_template.format(task_id=i, run_id=<run_id>, **kwargs)`.

   ```python
   from hpc_mapreduce import load_tasks_module, read_run_sidecar, tasks_path
   sidecar = read_run_sidecar(experiment_dir, run_id)
   tasks = load_tasks_module(tasks_path(experiment_dir))
   ```

   Recovery order if the local sidecar is missing:

   1. Pull from the cluster:
      ```bash
      mkdir -p .hpc/runs
      rsync -az $SSH_TARGET:$REMOTE_PATH/.hpc/runs/<run_id>.json ./.hpc/runs/<run_id>.json
      ```
   2. `.hpc/tasks.py` should already be in your local repo (it's git-tracked). If it's missing, the experiment was never properly scaffolded — re-run `/submit` and follow Step 6's scaffolding flow.
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

1. **Sanity-check before and after.** Run Step 0.5 pre-flight before arming the Monitor; run Step 4a post-flight before aggregating in Step 4b. File counts lie and stale job IDs waste hours — never skip these gates.
2. **Act autonomously on known failures.** For OOM, walltime, and node failures, immediately resubmit with appropriate resource overrides. Do NOT ask for permission. Only pause for code bugs or unrecognized errors.
3. **Stream, don't poll-loop.** Arm the Claude Code `Monitor` tool once (Step 5) with a script that emits one stdout line per state change. React to events as they arrive in chat — do **not** re-run `/status` from cron.

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
plain `/status` continues to work.

Keybinds:

| Key | Action |
|-----|--------|
| `r` | Force an immediate refresh (skips the poll interval) |
| `f` | Toggle focus on the failing-tasks panel |
| `l` | Open the currently focused task's error log via `ssh <host> less <log>` |
| `q` | Quit the TUI |

Invoke the module directly:

```bash
python -m hpc_mapreduce.reduce.tui \
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

Run **once** per `/status` invocation, immediately after Step 0 and **before** Step 1. If any check fails, report the specific failure and stop — do NOT proceed to Step 1 and do NOT arm the Monitor in Step 5.

Emit a single line `pre-flight: OK` on success, or `pre-flight FAILED: <which check>` on the first failure.

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
from hpc_mapreduce import load_tasks_module, tasks_path
sc = json.load(open(".hpc/runs/<run_id>.json"))
print(sc["sidecar_schema_version"], sc["task_count"])
print(load_tasks_module(tasks_path(".")).total())'
```

Verify `sidecar_schema_version >= 1` and `task_count` matches both `tasks.total()` and the value the user passed (or the sum of array sizes from squeue/qstat). A mismatch suggests the local `.hpc/tasks.py` was edited after submission; re-run `/submit` to write a fresh sidecar with the current task list.

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

## Step 1: Check Status

> **Anti-pattern (critical):** Do NOT use `ls logs/ | wc -l` or `find results/ -name '*.csv' | wc -l` as a success proxy. **Failed tasks produce logs and partial output too** — an `ImportError` still leaves an `.o*` file; a mid-run crash still leaves a `_wip_*` directory. Counting these as "progress" has led to hours of wasted cluster time discovering 100% failure rates after submission. Always either (a) invoke the status reporter below, or (b) `grep -clE 'Error|Traceback|ImportError' logs/*` and compute error-rate alongside any file count. **File existence is not success.**

Run the deterministic status reporter on the cluster. It reads `.hpc/runs/<run_id>.json` and `.hpc/tasks.py`, computes each task's `result_dir` from the sidecar's `result_dir_template` + `tasks.resolve(i)`, queries the scheduler (sacct for SLURM, qstat for SGE) for in-flight tasks, and emits a single JSON blob:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && python -m hpc_mapreduce.reduce.status \
    --run-id <run_id> \
    --job-ids <comma_separated_job_ids> \
    --job-name <profile_name> \
    --log-dir logs \
    --file-glob "<results.summary_pattern or *>"'
```

The returned JSON has:
- `summary`: `{complete, running, pending, failed, unknown}` counts across all tasks
- `tasks`: per-task `{status, ...}` with 1-based task IDs
- `rollup`: per-grid-point counts (grouped by the kwargs returned by `tasks.resolve(i)`), keyed by a stable `k=v` string

Format the `rollup` and `summary` into the user-facing table:

```
Grid point status:
  ridge_h1_2020-01:   complete
  ridge_h1_2020-07:   complete
  ridge_h5_2020-01:   running
  xgboost_h1_2020-01: failed

Overall: 3/6 tasks complete, 2 running, 1 failed
```

Do NOT re-count result files via `ls | wc -l`; the reporter already does this deterministically and the LLM's role here is presentation, not counting.

Parse the `summary` to determine state:

| Condition | State | Action |
|-----------|-------|--------|
| completed == total_tasks | `all_complete` | Go to Step 4a |
| running > 0 or pending > 0 | `still_running` | Check for stalls (Step 1b), then Step 5 |
| failed > 0 and running == 0 | `has_failures` | Go to Step 2 |
| completed == 0 and running == 0 | `all_failed` | Go to Step 2 (triage carefully) |

### Step 1b: Detect Queue Stalls

**Stall heuristic**: If ALL tasks have been pending for >15 minutes with zero running, or if the state is unchanged across 2 consecutive checks, treat as a stall. Go to Step 2 with category `queue_stall`.

### Step 1c: Run Combiners for Completed Waves

If the run sidecar contains a `wave_map` field, run the on-cluster combiner for each newly-completed wave. This aggregates metrics on the cluster while later waves are still running (pipeline parallelism).

1. Read `wave_map` from the sidecar to determine which task IDs belong to each wave
2. Cross-reference with the Step 1 reporter's `tasks` dict: a wave is **complete** when every task ID in the wave has `status == "complete"` in the `tasks` dict (no separate re-count needed)
3. Check `combined_waves` from the monitor state to skip waves already combined
4. For each newly-complete wave not yet combined, invoke `runner.combine_wave`. The wrapper records `combined_waves`/`failed_waves` atomically — the slash command must NOT call `update_run_status` directly for these fields:
   ```python
   from pathlib import Path
   from slash_commands import runner

   ok, stdout, stderr = runner.combine_wave(
       Path.cwd(), run_id,
       wave=N,
       ssh_target=SSH_TARGET,
       remote_path=REMOTE_PATH,
   )
   ```
5. On `ok=True`, the wrapper has already appended the wave to `combined_waves`. Report:
   `"Combiner: wave <N> aggregated — <grid_point>: <key_metric>=<value>, ..."`
6. On `ok=False`, the wrapper has already appended the wave to `failed_waves` with the stderr excerpt. **Never** mark a failed wave as combined.

**Failure policy:**
- 1st failure: retry automatically on the next monitoring tick via `runner.combine_wave(..., force=True)`.
- 2nd failure: surface to the user with the stderr excerpt and stop retrying until the user intervenes.

If the sidecar has no `wave_map` field, skip this step (backward compatibility with older submissions).

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
| Node failure / `Eqw` / `NODE_FAIL` | Infra issue | Resubmit as-is |
| All tasks pending >15min / unchanged across 2 checks | Queue stall | Delete stalled job, resubmit with GPU fallback |
| Python traceback with clear bug | Code bug | **STOP. Report to user. Do NOT resubmit.** |
| Unrecognized error | Unknown | **STOP. Read full log, report to user.** |

**AUTONOMY RULE**: For OOM, walltime, node failures, and queue stalls — act immediately. Only STOP for code bugs and unrecognized errors.

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

After the resubmission backend call returns the new job IDs, record the retry attempt in the journal:

```python
from slash_commands import runner

runner.resubmit_failed(
    Path.cwd(), run_id,
    failed_task_ids=<list of task ids being resubmitted>,
    category=<failure category from classify_failure, e.g. "system_oom">,
    overrides=<resource overrides applied>,
    new_job_ids=<list of new job ids returned by the backend>,
)
```

The wrapper increments `retries[tid].attempts` and records category + overrides. A future cold session sees the retry count and won't blow past `max_retries`.

## Step 4a: Post-flight Verification

Run **once** when Step 1 reports `all_complete`, **before** any aggregation in Step 4b. If any check fails, emit `post-flight FAILED: <which check>` and stop — do NOT invoke `results.aggregate_cmd`, do NOT auto-claim completion. The job may have produced files but not real results.

This guards against the "file count lies" failure mode (see anti-pattern at Step 1) — `summary.complete == total_tasks` from the status reporter is a necessary but not sufficient condition for success.

### 4a.1 — Non-empty rows

Re-run the status reporter with `--min-rows N` (the existing CLI flag — see `docs/cli-contract.md`) where `N` is a profile-appropriate floor (1 minimum, more if the profile knows the expected row count).

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && python -m hpc_mapreduce.reduce.status \
    --run-id <run_id> \
    --job-ids '"$JOB_IDS"' --job-name '"$NAME"' --log-dir logs \
    --file-glob "'"$SUMMARY_PATTERN"'" \
    --min-rows '"$MIN_ROWS"
```

Any task that previously read `complete` but flips to `failed` here had an empty/short result file. Report which task IDs failed.

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

If the current stage completes and another stage has `depends_on` pointing to this stage, check the `depends_on` graph for newly unblocked stages. For each unblocked stage, prompt: "Stage `<next_stage>` is now unblocked (depends on `<this_stage>`). Submit it? (`/submit <profile_name>.<next_stage>`)"

## Step 5: Arm the Monitor

**Skip if state is `all_complete` (Step 4b done) or fully abandoned.** Report done and stop.

Otherwise, arm the Claude Code `Monitor` tool **once** with a poll script that streams state changes back to chat. Do **not** re-run `/status` from cron — react to events as they arrive.

### When to (re)arm

- After Step 1 baseline if state is `still_running`.
- After every Step 3 resubmission (job IDs change → call `TaskStop` on the prior monitor first, then arm a fresh one with the new `$JOB_IDS`).
- After Step 1c combiner runs (wave list updated, but job IDs unchanged → reuse the existing monitor; only restart if it has already exited).

### Adaptive poll interval

Pick the `sleep` value baked into the script using Step 1's baseline:

| Condition | Interval |
|-----------|----------|
| < 10% complete | 60s |
| ETA < 10 min | 60s |
| ETA 10–30 min, stable pace | 180s |
| ETA 10–30 min, unstable pace | 90s |
| ETA > 30 min, stable pace | 300s |
| ETA > 30 min, unstable pace | 180s |

Fallback (no progress data): 90s if anything is running, 180s if all pending.

### Monitor script

Fill in `$SSH_TARGET`, `$REMOTE_PATH`, `$JOB_IDS`, `$NAME`, `$INTERVAL` at arm-time, then pass the resulting command to the `Monitor` tool with `persistent: true` and `description: "HPC <profile> state changes"`.

The script emits one stdout line **only when state changes** — task transitions, new failures, newly-complete waves, terminal state, or a status-query failure. Silence means "no change since last tick," not "still running with nothing happening."

```bash
prev=""
prev_failed=0
prev_complete=0
while true; do
  raw=$(ssh -o IdentitiesOnly=yes "$SSH_TARGET" "cd $REMOTE_PATH && \
    python -m hpc_mapreduce.reduce.status \
      --run-id <run_id> \
      --job-ids $JOB_IDS --job-name $NAME --log-dir logs" 2>&1) \
    || { echo "STATUS_QUERY_FAILED $(printf %s "$raw" | head -1)"; sleep "$INTERVAL"; continue; }

  cur=$(jq -rc '.summary | "c=\(.complete) r=\(.running) p=\(.pending) f=\(.failed)"' <<<"$raw" 2>/dev/null) \
    || { echo "STATUS_PARSE_FAILED"; sleep "$INTERVAL"; continue; }

  if [ "$cur" != "$prev" ]; then
    echo "STATE $cur"

    new_failed=$(jq '.summary.failed' <<<"$raw")
    if [ "$new_failed" -gt "$prev_failed" ]; then
      jq -r '.tasks | to_entries[] | select(.value.status=="failed") | .key' <<<"$raw" \
        | tail -$(( new_failed - prev_failed )) \
        | sed 's/^/NEW_FAILURE tid=/'
    fi

    jq -r '.rollup | to_entries[]
        | select(.value.complete == .value.total and .value.total > 0)
        | "WAVE_READY \(.key)"' <<<"$raw"

    prev=$cur
    prev_failed=$new_failed
    prev_complete=$(jq '.summary.complete' <<<"$raw")
  fi

  total=$(jq '.summary | (.complete+.running+.pending+.failed+.unknown)' <<<"$raw")
  done=$(jq  '.summary.complete' <<<"$raw")
  run=$(jq   '.summary.running'  <<<"$raw")
  fail=$(jq  '.summary.failed'   <<<"$raw")

  [ "$done" = "$total" ]               && { echo "TERMINAL all_complete"; break; }
  [ "$fail" -gt 0 ] && [ "$run" = 0 ]  && { echo "TERMINAL has_failures"; break; }

  sleep "$INTERVAL"
done
```

### Reacting to events

When a notification arrives:

| Event | Action |
|-------|--------|
| `STATE c=… r=… p=… f=…` | Update internal counters; no action unless paired with another event below. |
| `NEW_FAILURE tid=<n>` | Read `.err`/`.out` for that task (Step 2), classify with `classify_failure`, and resubmit per Step 3 if the category is auto-handled. After resubmit, `TaskStop` this monitor and re-arm with new `$JOB_IDS`. |
| `WAVE_READY <grid_point_key>` | Run the on-cluster combiner (Step 1c). Update `combined_waves`. |
| `TERMINAL all_complete` | Go to Step 4a (post-flight verification), then Step 4b (aggregate), then Step 6 (report). Then call `runner.mark_terminal(Path.cwd(), run_id, status='complete', stage='aggregate')` so the journal reflects the terminal state and `find_in_flight_runs` no longer returns this run. |
| `TERMINAL has_failures` | Go to Step 2 (diagnose), then Step 3 (resubmit) or escalate. If the user explicitly abandons the run, call `runner.mark_terminal(Path.cwd(), run_id, status='failed')`. Otherwise leave the status as `in_flight` so the next session can resume. |
| `STATUS_QUERY_FAILED …` / `STATUS_PARSE_FAILED` | Transient — first occurrence: ignore. Second consecutive: investigate (SSH dead? cluster down?) before re-arming. |

### Stall detection

The Monitor's silence is now meaningful: if no event arrives for **15 × `$INTERVAL`** (or 15 minutes, whichever is greater) AND Step 1 baseline showed all-pending/zero-running, treat as queue stall (existing Step 2 category). Use a `ScheduleWakeup` to check back at that horizon if you have other work to do meanwhile.

## Step 6: Report

Always end with a concise summary:
- Grid point completion breakdown
- Tasks: X/Y complete, Z running, W failed
- Actions taken this iteration (if any)
- Next: waiting / needs attention / done

## Context Management

1. **Single in-session loop**: One `/status` invocation arms one Monitor and stays in the same conversation until `TERMINAL`. There is no cron handoff and no fresh-context replay — the Monitor stream IS the loop.
2. **Don't re-poll on a tick.** The Monitor script is the only thing polling. The agent reacts to events; it does not run its own status reporter on a timer.
3. **Compact event log**: When summarizing for the user, collapse repeated `STATE …` lines into deltas; surface only `NEW_FAILURE`, `WAVE_READY`, and `TERMINAL` lines verbatim.
4. **Minimize tool output**: Use `tail -20` for logs. Prefer compact status commands over verbose output.
5. **If the session ends**: monitoring stops. Re-run `/status` (no args) — the run journal at `~/.claude/hpc/<repo_hash>/` will surface the in-flight run with last-known status; on Y, `runner.reconcile` re-derives ground truth before re-arming the Monitor.
