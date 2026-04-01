Monitor running HPC jobs via SSH and take corrective action.

## Setup

Read both config files:
- `hpc.yaml` in the current working directory
- `clusters.yaml`: resolve path via `python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from the configs. If `$ARGUMENTS` contains `--cluster <name>`, use that cluster instead of the top-level `cluster` field. If `$ARGUMENTS` contains `--profile <name>`, use that profile; otherwise infer from context or ask.

Read `_hpc_dispatch.json` (locally if available, or from the cluster via SSH) to load the task-to-grid-point mapping. Each task ID maps to a grid point and optional chunk: `grid_point = task_id // chunks_per_point`, `chunk = task_id % chunks_per_point`.

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

1. **Act autonomously on known failures.** For OOM, walltime, and node failures, immediately resubmit with appropriate resource overrides. Do NOT ask for permission. Only pause for code bugs or unrecognized errors.
2. **Compact context each iteration.** Summarize all prior monitoring output into a single state block before scheduling the next check.
3. **Self-loop.** After each monitoring cycle, schedule the next check using `CronCreate` with an adaptive interval.

## Arguments

$ARGUMENTS formats (pick one):

1. **Profile + monitor** (no job-ids — checks active jobs for the profile):
   `<profile_name>` or `<profile_name> --cluster <name>`
   For multi-stage profiles: `<profile_name>.<stage_name>`

2. **Monitor existing** (job-ids provided):
   `<profile_name> <job_ids> [total_tasks]`
   Example: `ml 12345678,12345679 1200`

3. **Auto-discover** (empty):
   Check for active jobs belonging to the current project via queue status commands. Cross-reference with profile names from `hpc.yaml` to identify which profiles are running.

## Step 1: Check Status

Run the appropriate scheduler query (qstat for SGE, sacct for SLURM) and count completed results per grid point using `results.dir` and `results.pattern` from the profile:

```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<results.dir>/<results.pattern> 2>/dev/null | wc -l'
```

Use `_hpc_dispatch.json` to map each task ID back to its grid point and chunk. Report completion per grid point:

```
Grid point status:
  ridge_har:      98/100 chunks complete
  ridge_pca:      100/100 chunks complete ✓
  xgboost_har:    45/100 chunks complete, 2 failed
  xgboost_pca:    0/100 chunks (all pending)

Overall: 243/400 tasks complete, 155 running, 2 failed
```

Parse results to determine state:

| Condition | State | Action |
|-----------|-------|--------|
| completed == total_tasks | `all_complete` | Go to Step 4 |
| running > 0 or pending > 0 | `still_running` | Check for stalls (Step 1b), then Step 5 |
| failed > 0 and running == 0 | `has_failures` | Go to Step 2 |
| completed == 0 and running == 0 | `all_failed` | Go to Step 2 (triage carefully) |

### Step 1b: Detect Queue Stalls

**Stall heuristic**: If ALL tasks have been pending for >15 minutes with zero running, or if the state is unchanged across 2 consecutive checks, treat as a stall. Go to Step 2 with category `queue_stall`.

## Step 2: Diagnose Failures

Read error logs for failed tasks (tail -50 from the appropriate log path).

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

Build the resubmission command using the same dispatch mechanism. The task IDs in the resubmission correspond to the same `_hpc_dispatch.json` entries. Apply resource overrides to the submission flags.

**Update your job-ids list** for subsequent status checks.

## Step 4: Aggregate (if configured)

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

## Step 5: Schedule Next Check

**Skip if `all_complete` or fully abandoned.** Report done and stop.

### Adaptive wait interval

| Condition | Interval | Reason |
|-----------|----------|--------|
| < 10% complete | 3 min | pace still settling |
| ETA < 10 min | 3 min | finishing soon |
| ETA 10-30 min, stable pace | 10 min | stable, moderate time |
| ETA 10-30 min, unstable pace | 5 min | fluctuating, moderate time |
| ETA > 30 min, stable pace | 15 min | stable, long run |
| ETA > 30 min, unstable pace | 10 min | fluctuating, long run |

Fallback (no progress data):

| State | Interval |
|-------|----------|
| All pending, none running | 5 min |
| Some running, no progress yet | 3 min |
| Just resubmitted failed tasks | 3 min |
| Unchanged from previous check | double previous interval (cap 15 min) |

### Schedule via CronCreate

1. Cancel any existing monitor cron job.
2. Create a one-shot cron at current time + interval.
3. The prompt must include full state for the next iteration:

```
/monitor <profile_name> <comma_separated_job_ids> <total_tasks>

[Monitor State] profile=<name> | cluster=<cluster> | tasks=X/Y done, Z running, W failed | grid_points: {point: done/total, ...} | retries: {task: count, ...} | jobs: <id_list> | gpu_type: <current_gpu> | last_check: <time> | prev_interval: <minutes> | consecutive_pending: <count>
```

4. Report: `Next check in X min (reason). Cron job: <id>`

## Step 6: Report

Always end with a concise summary:
- Grid point completion breakdown
- Tasks: X/Y complete, Z running, W failed
- Actions taken this iteration (if any)
- Next: waiting / needs attention / done

## Context Management

1. **Within a conversation**: Avoid re-reading data already in context. Summarize before scheduling.
2. **Cron handoff**: Each CronCreate starts fresh. The prompt must carry all state.
3. **Minimize tool output**: Use `tail -20` for logs. Prefer compact status commands over verbose output.
