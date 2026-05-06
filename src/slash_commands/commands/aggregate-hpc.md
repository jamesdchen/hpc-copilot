Help me aggregate, validate, and analyze experiment results using the project configuration.

Per-operation contracts live in `docs/primitives/` — this skill composes [combine-wave](../../docs/primitives/combine-wave.md) (per-wave aggregation) plus the surface-specific multi-wave orchestration, partial-pull, and final-aggregate flow. For envelope/exit-code shapes see `docs/reference/cli-spec.md`.

Aggregation runs on the cluster to avoid transferring many result files. Only summary files are downloaded locally.

## Core Principle: Reduce Where the Data Lives

**Never move bulk result files to reach a Python env.** If the reduction is trivial (pandas concat, `optuna.tell()`, JSON dump) but the host with the data lacks the deps, install the deps on that host — a 30s `pip install` beats minutes of small-file scp/rsync.

Decision rule before any `scp`/`rsync` of results:

1. **Is the compute genuinely HPC-scale?** (GPU, >1 node, hours of CPU) → run on cluster, aggregate on cluster, pull summaries.
2. **Is the compute trivial?** (pandas, sqlite, scalar output) → run it wherever the data already sits. Install missing deps in place.
3. **Must data actually move?** → move the *small* side (params/code down, reduced output up). Never bulk-push raw chunks between clusters to reach an env.

Anti-pattern: `scp -r results/tune/*_chunk_*.csv cluster-B:...` because cluster-B has the conda env and cluster-A doesn't. Fix the env, not the data location.

Small-file scp/rsync over SSH is especially slow (per-file TCP/SSH handshake). If bulk movement is truly unavoidable, `tar` first.

## Setup

Read cluster definitions:
- `clusters.yaml`: resolve path via `python -c 'from claude_hpc import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Determine cluster and connection:
- If `$ARGUMENTS` contains `--cluster <name>`, use that cluster
- Else read `cluster` from the most recent matching `.hpc/runs/<run_id>.json` sidecar
- Else check Claude Code memory for cached cluster preference
- Else ask the user

Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from cluster config + cached/configured remote path.

Load the run's identity and task definition. Two files together describe the run:

- `.hpc/runs/<run_id>.json` — the per-run sidecar: cmd_sha, executor, `result_dir_template`, task_count, wave_map.
- `.hpc/tasks.py` — the user's `total()` / `resolve(task_id)` module. Per-task kwargs (the "grid point") come from `tasks.resolve(i)`; per-task `result_dir` is the sidecar's template formatted against `task_id` + `run_id` + kwargs.

Pull the sidecar locally if missing:

```bash
mkdir -p .hpc/runs
rsync -az $SSH_TARGET:$REMOTE_PATH/.hpc/runs/<run_id>.json ./.hpc/runs/<run_id>.json
```

`.hpc/tasks.py` is git-tracked; it should already be in your local repo.

## SSH Quoting

Single-quote the remote command so variables expand on the cluster, not locally:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && echo $SGE_TASK_ID'
```

## Arguments

$ARGUMENTS formats:

1. **Profile + stage**: `<profile_name>` or `<profile_name>/<stage_name>`
2. **Empty**: auto-discover which profiles/stages have completed results ready for aggregation

## Step 0: Run `aggregate-flow` (combiner partials path)

The `/monitor-hpc` loop may have already combined some waves during execution; whatever's missing, the `aggregate-flow` workflow atom finishes. **One CLI call** does: ensure every wave is combined (via `combine-wave` for any missing) → rsync `_combiner/` partials locally → `reduce_partials` to produce aggregated metrics → optionally pull per-task summaries.

```bash
hpc-mapreduce aggregate-flow --spec .hpc/runs/<run_id>.aggregate.spec.json --experiment-dir .
```

Spec shape (matches `schemas/aggregate_flow.input.json`):

```json
{
  "run_id": "<run_id>",
  "ensure_all_combined": true,
  "combiner_max_retries": 1,
  "pull_summaries": true,
  "summary_glob": "<results.summary_pattern>",
  "results_subdir": "results"
}
```

Parse `data.aggregated_metrics` — that's the cross-wave reduced output. `data.combiner_dir_local` and `data.summaries_dir_local` are the local paths if downstream interpretation needs them.

**Verify the framework-knowable invariants** via the `verify-aggregation-complete` primitive before reporting to the user:

```bash
hpc-mapreduce verify-aggregation-complete \
    --experiment-dir . \
    --run-id "$RUN_ID" \
    --combiner-dir "$COMBINER_DIR_LOCAL"
```

The envelope's `data` carries `{ok, all_waves_combined, missing_waves, all_tasks_present, missing_tasks, unexpected_tasks, provenance_present, ...}`. Branch:

- `ok=True` → proceed to interpretation (Step 6).
- `ok=False` → surface the specific violations (`missing_waves` / `missing_tasks` / `unexpected_tasks` / `provenance_present`) before any user-facing framing. `unexpected_tasks` in particular is a cross-run contamination red flag — escalate, don't paper over.

If `data.escalation_reason` from `aggregate-flow` is set (e.g. `combiner_failed_max_retries:waves=3,7`), surface to the user and decide whether the partial aggregation is acceptable. The atom proceeds with whatever waves DID combine; the caller decides whether the result is usable.

**Skip to Step 4** if the profile defines an `aggregate_defaults.aggregate_cmd` (an arbitrary user-defined cluster-side command that the framework doesn't know about) — that's the only step `aggregate-flow` doesn't replace.

## Step 1: Identify What to Aggregate

Load `.hpc/runs/<run_id>.json` and `.hpc/tasks.py` to understand the submission structure:

```python
from claude_hpc import load_tasks_module, read_run_sidecar, tasks_path
sidecar = read_run_sidecar(experiment_dir, run_id)
tasks = load_tasks_module(tasks_path(experiment_dir))
n = tasks.total()
```

Each task's grid point is `tasks.resolve(i)` (a kwargs dict); each task's `result_dir` is `sidecar["result_dir_template"].format(task_id=i, run_id=run_id, **tasks.resolve(i))`.

```
Submission summary (from sidecar + tasks.py):
  Run ID:        ml_ridge-20260429-153012-abc12345
  Executor:      python3 src/ml_ridge.py
  Tasks:         60
  Grid kwargs:   {executor, horizon, window_start, window_end}
  Sample dir:    results/ml_ridge_h1_2020-01/
```

If `$ARGUMENTS` specifies an executor or result directory, use it. Otherwise, present the kwargs structure from `tasks.py` and ask what to aggregate.

If a recent run sidecar's `aggregate_defaults.aggregate_cmd` is set for a matching profile, use it. Otherwise, discover aggregation scripts in the repo or ask the user what aggregation command to run.

## Step 2: Check Job Status

Before aggregating, confirm all jobs have finished by checking the queue (qstat for SGE, squeue for SLURM).

If jobs are still running for the selected profile/stage, report which ones and wait. Do NOT aggregate partial results unless explicitly asked.

## Step 3: Validate Task Completeness

**Preferred path: let `hpc-mapreduce aggregate` enforce this for you.**
The CLI accepts `--require-outputs <template>` (with `{task_id}` placeholder)
which resolves the template against the run sidecar's `wave_map`,
SSH-checks every per-task output, and refuses to combine if any are
missing. The error envelope reports `error_code: outputs_missing` with the
list of absent paths. Set the default per-run via `write_run_sidecar(...,
aggregate_defaults={"require_outputs": "...", "expect_output": "..."})`
at /submit-hpc time so every aggregate is guarded automatically.

When you must validate manually (e.g., older repos without sidecar defaults):

```bash
# For each task, check if result files exist
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<task_result_dir>/<result_pattern> 2>/dev/null | wc -l'
```

Report per-grid-point completeness:

```
Task completeness:
  ridge_h1:       10/10 tasks complete
  ridge_h5:       10/10 tasks complete
  xgboost_h1:     8/10 tasks complete — MISSING tasks: 3, 7
  xgboost_h5:     10/10 tasks complete
```

**If tasks are missing results:**

1. Identify which task IDs are missing by cross-referencing `tasks.total()` with existing result directories (one per `tasks.resolve(i)` formatted through the sidecar's `result_dir_template`).
2. Check job accounting for failure reasons (qacct for SGE, sacct for SLURM).
3. Check error logs (tail -50).
4. Report findings and suggest resubmitting via `/submit-hpc` or monitoring via `/monitor-hpc` for gaps.
5. Wait for resubmitted jobs, then re-validate before aggregating.

**Partial aggregation:** Only proceed when all expected task results are present, unless the user explicitly asks to aggregate partial results. If partial, note the missing count and percentage per grid point.

**No partial-bucket leaderboards.** For tuning/sweep workflows (e.g., optuna studies, trial-id grids), **do not** compute or report a "best QLIKE / best score / ranking" until every trial in the bucket is 100% complete. A "best so far" reorders as more trials land — showing it invites premature conclusions and contaminates downstream analysis. If the user explicitly asks for a partial leaderboard, label every number as provisional and list the trials still outstanding.

## Step 4: Aggregate on Cluster

Determine the aggregation command:
1. If a recent run sidecar's `aggregate_defaults.aggregate_cmd` is set for the relevant profile → use it.
2. Else invoke the **`discover-reducers`** primitive — DO NOT grep / write a fresh reducer first:
   ```bash
   hpc-mapreduce discover-reducers --experiment-dir .
   ```
   The envelope's `data.reducers` is a list of candidate `.py` files matched by filename stem (`aggregate.py`, `qlike.py`, `score.py`, etc.) or top-level function names (`def aggregate(...)`, `def reduce(...)`, `def score(...)`). Each entry carries `path`, `matches` (the signals that hit, e.g. `["name:qlike", "function:aggregate"]`), and the first line of the module docstring. Multi-signal hits sort first.
   - **One candidate** that obviously matches the loss the user asked for → use it as `aggregate_cmd` and confirm with one short sentence.
   - **Multiple candidates** → list them (path + docstring + matches) and ask the user which one. Don't pick silently.
   - **Zero candidates** → fall through to step 3.
3. Else ask the user: "I didn't find an existing reducer matching `<loss>` in `<repo>` (`hpc-mapreduce discover-reducers` returned nothing). Should I write one, or do you have an aggregation command I should use?" Surface that you searched explicitly so they don't assume you skipped the step.

Writing a fresh reducer when one already exists is a common failure mode — the user has historically committed loss functions like QLIKE / RMSE / MAE under `scripts/`, `aggregators/`, `src/eval/`, etc., and a fresh one duplicates code AND drifts from the canonical implementation. The `discover-reducers` primitive exists specifically to bridge that gap; route through it.

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
  xgboost_har:    incomplete (8/10 tasks)
  xgboost_pca:    complete — QLIKE: 0.310, MSE: 0.0011

Cluster cost: 47.2 CPU-hours, 3.1 GPU-hours (60 tasks counted)
```

### Cluster cost rollup

`/monitor-hpc`'s status report exposes a top-level `resource_usage` key:

```json
{"cpu_hours": 47.2, "gpu_hours": 3.1, "elapsed_hours": 12.4, "tasks_counted": 60}
```

Values are derived from `sacct` (`ElapsedRaw * ReqCPUS`, `gres/gpu` in
`AllocTRES`) or `qacct` (`ru_wallclock * slots`, `gpu=N` in the hard
resource list).  Surface these numbers after the per-grid-point metrics
so the user knows what a given sweep cost in cluster time — no dollar
conversion, just hours.

When interpreting:
- Lead with the most important metric or finding
- Flag anomalies (empty results, unexpected values, low sample counts)
- Sort metrics alphabetically by default; CLI flags or memory may override the order
- Compare against any baseline results if available
- Group results by grid dimensions for readability (e.g., by model, by feature set)

## Step 7: Mark the run complete in the journal

After aggregation succeeds and summaries are downloaded, finalize the run
journal so `find_in_flight_runs` no longer surfaces this run on the next
`/monitor-hpc` invocation:

````python
from pathlib import Path
from slash_commands import session, runner

# Hydrate run_id from the active context, or pick from the in-flight set.
in_flight = session.find_in_flight_runs(Path.cwd())
if len(in_flight) == 1:
    run_id = in_flight[0].run_id
elif len(in_flight) == 0:
    # Nothing to mark — likely the run was already finalized or never recorded.
    run_id = None
else:
    # Multiple in-flight runs share this cwd. Prompt the user to pick the
    # one that this aggregate call corresponds to (match by profile, run_id,
    # or job_name).
    run_id = <user's choice>

if run_id is not None:
    runner.mark_terminal(Path.cwd(), run_id, status='complete', stage='done')
````

If aggregation FAILS (e.g., cluster aggregate command exits non-zero, summary
files are missing, key metrics fail validation), do NOT call `mark_terminal`.
Leave the journal entry as `in_flight` so the user can re-run `/aggregate-hpc`
once the issue is fixed, or transition to manual triage.

For multi-executor submissions (one journal entry per submitted job), call
`mark_terminal` once per `run_id` whose aggregation succeeded.

## Multi-Stage Aggregation

If the profile has multiple stages and `$ARGUMENTS` does not specify a stage:

1. Check all stages for completeness
2. Aggregate stages in dependency order — stages with `depends_on` must wait until their dependencies are aggregated first
3. Report results for each stage separately
4. If a dependency stage is incomplete, skip downstream stages and report the blockage
