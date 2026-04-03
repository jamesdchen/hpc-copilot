Help me aggregate, validate, and analyze experiment results using the project configuration.

Aggregation runs on the cluster to avoid transferring many chunk files. Only summary files are downloaded locally.

## Setup

Read cluster definitions:
- `clusters.yaml`: resolve path via `python -c 'from hpc_mapreduce import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Determine cluster and connection:
- If `$ARGUMENTS` contains `--cluster <name>`, use that cluster
- Else if `hpc.yaml` exists, read `cluster` field
- Else check Claude Code memory for cached cluster preference
- Else ask the user

Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from cluster config + cached/configured remote path.

Read `_hpc_dispatch.json` (locally if available, or from the cluster via SSH) to understand the grid structure: task-to-grid-point mapping, result directories per grid point, and chunk counts. This is the **primary source of truth** for what was submitted.

## SSH Quoting

Single-quote the remote command so variables expand on the cluster, not locally:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && echo $SGE_TASK_ID'
```

## Arguments

$ARGUMENTS formats:

1. **Profile + stage**: `<profile_name>` or `<profile_name>/<stage_name>`
2. **Empty**: auto-discover which profiles/stages have completed results ready for aggregation

## Step 1: Identify What to Aggregate

Read `_hpc_dispatch.json` to understand the submission structure:

```
Submission summary (from dispatch manifest):
  Grid keys: [executor, horizon]
  Grid points: 6
  Chunks per point: 100
  Total tasks: 600
  Result directories: results/ml_ridge_1/, results/ml_ridge_5/, ...
```

If `$ARGUMENTS` specifies an executor or result directory, use it. Otherwise, present the grid structure from the dispatch manifest and ask what to aggregate.

If `hpc.yaml` exists and has `results.aggregate_cmd` for a matching profile, use it. Otherwise, discover aggregation scripts in the repo or ask the user what aggregation command to run.

## Step 2: Check Job Status

Before aggregating, confirm all jobs have finished by checking the queue (qstat for SGE, squeue for SLURM).

If jobs are still running for the selected profile/stage, report which ones and wait. Do NOT aggregate partial results unless explicitly asked.

## Step 3: Validate Chunk Completeness

Check each grid point's result directory for expected chunks. Use `_hpc_dispatch.json` to determine the result directory and expected chunk count per grid point.

```bash
# For each grid point, count completed results
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<grid_point_result_dir>/<result_pattern> 2>/dev/null | wc -l'
```

Report per-grid-point completeness:

```
Chunk completeness:
  ridge_har:      100/100 complete
  ridge_pca:      100/100 complete
  xgboost_har:    95/100 complete — MISSING chunks: 12, 37, 44, 88, 91
  xgboost_pca:    100/100 complete
```

**If chunks are missing:**

1. Identify which chunk IDs are missing by listing what exists and computing the gaps.
2. Check job accounting for failure reasons (qacct for SGE, sacct for SLURM).
3. Check error logs (tail -50).
4. Report findings and suggest resubmitting via `/submit` or monitoring via `/monitor` for gaps.
5. Wait for resubmitted jobs, then re-validate before aggregating.

**Partial aggregation:** Only proceed when all expected chunks are present, unless the user explicitly asks to aggregate partial results. If partial, note the missing count and percentage per grid point.

## Step 4: Aggregate on Cluster

Determine the aggregation command:
1. If `hpc.yaml` exists and defines `results.aggregate_cmd` for the relevant profile → use it
2. Else look for aggregation scripts in the repo (e.g., `scripts/aggregate.py`, `src/evaluation.py`)
3. Else ask the user: "What command should I run to aggregate results?"

Run the aggregation command on the cluster. The command may operate per grid point (with `RESULT_DIR` set to each grid point's result directory) or globally if the command handles discovery itself.

```bash
# Per grid point:
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && RESULT_DIR=<grid_point_result_dir> <aggregate_cmd>'

# Or globally:
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && <aggregate_cmd>'
```

If the aggregate command's options are unclear, invoke it with `--help` to discover available flags:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && <aggregate_cmd> --help'
```

Verify the command succeeds (exit code 0). If it fails, read stderr and report to user.

## Step 5: Download Summaries

After aggregation completes, pull summary files from all grid point result directories:

```bash
rsync -az \
    --include='*/' \
    --include='<results.summary_pattern>' \
    --exclude='*' \
    $SSH_TARGET:$REMOTE_PATH/<result_base_dir>/ ./<result_base_dir>/
```

If `results.summary_pattern` is a list, include each pattern. Verify downloaded files exist locally.

## Step 6: Interpret Results

After downloading, read the local summary files and report per-grid-point results.

```
Aggregation results:
  ridge_har:      complete — QLIKE: 0.342, MSE: 0.0012
  ridge_pca:      complete — QLIKE: 0.298, MSE: 0.0010
  xgboost_har:    incomplete (95/100 chunks)
  xgboost_pca:    complete — QLIKE: 0.310, MSE: 0.0011
```

When interpreting:
- Lead with the most important metric or finding
- Flag anomalies (empty results, unexpected values, low sample counts)
- If `hpc.yaml` defines a `metrics` section, use those metric names and their sort order
- Compare against any baseline results if available
- Group results by grid dimensions for readability (e.g., by model, by feature set)

## Multi-Stage Aggregation

If the profile has multiple stages and `$ARGUMENTS` does not specify a stage:

1. Check all stages for completeness
2. Aggregate stages in dependency order — stages with `depends_on` must wait until their dependencies are aggregated first
3. Report results for each stage separately
4. If a dependency stage is incomplete, skip downstream stages and report the blockage
