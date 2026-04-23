Help me submit HPC jobs via SSH. Discovers experiment executors, builds submission plans conversationally, and handles all deployment.

CLI shapes for every tool referenced below: see `docs/cli-contract.md`.

All cluster commands run remotely via SSH. Code is synced from the local machine before submission.

## Setup

Read cluster definitions:
- `clusters.yaml`: resolve path via `python -c 'from hpc_mapreduce import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Check for existing context (in priority order):

1. **Previous submission**: If `_hpc_dispatch.json` exists locally, read it. Offer: "Previous submission: [summary of grid, tasks, cluster]. Resubmit same, modify, or start fresh?"
   - **Resubmit same** → skip to Step 5 (sync + submit)
   - **Modify** → pre-populate from dispatch manifest, go to Step 3 (adjust grid/config)
   - **Start fresh** → continue to Step 1

2. **hpc.yaml exists**: Read it as optional context. If it has `profiles`, offer: "I see profiles: [list]. Use one, or build a new submission?" If using a profile, extract its `run`, `grid`, `constraints`, `env_group`, and `resources` as defaults and skip to Step 3 for confirmation (a `backtest:` block, if present, is translated to a generated date-window shim at Step 3 — see below).

3. **Neither exists**: Continue to Step 1 (full discovery).

## SSH Quoting

Single-quote the remote command so variables expand on the cluster, not locally:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && echo $SGE_TASK_ID'
```

## Step 1: Discover Executors

Call the shared discovery helper — identical to what `/build-executor` uses so both commands see the same set of executors:

```python
from hpc_mapreduce import discover_executors
execs = discover_executors(".")   # returns list[ExecutorInfo]
```

`discover_executors` scans `executors/`, `scripts/`, and `src/` (in that order, collecting from every one that exists) and falls back to the repo root if none are present. The contract is: a file is an executor iff it parses and has both an `if __name__ == "__main__":` guard and a CLI import (`argparse`, `click`, `typer`, or `fire`) — utilities, `__init__.py`, and reserved HPC filenames are filtered out automatically. Each returned `ExecutorInfo` has `path`, `name`, `cli_framework`, `imports`, and `docstring`.

Cache the resolved directory in Claude Code memory for this project. If the cached directory differs from the defaults, pass it through `search_dirs=(...)`. If the user explicitly names a different directory, honor it the same way.

For each executor, run `python3 <info.path> --help` to map its CLI interface (the helper parses source but does not execute it). Parse:
- Grid-able parameters (model hyperparams, feature types, etc.)
- Data arguments (`--data-path`, `--horizon`, `--start`, `--end`)
- Output arguments (`--output-file`)

Present the inventory (use `info.name` and `info.path` as the identifiers; `info.docstring` is handy for the one-line summary):

```
Executors found in src/:
  ml_ridge.py     — args: --horizon, --data-path, --train-window, --start, --end, --output-file
  ml_xgboost.py   — args: --horizon, --data-path, --train-window, --start, --end, --output-file
  dl_patchts.py   — args: --horizon, --data-path, --gpu-count, --start, --end, --output-file

Which do you want to run?
```

If `discover_executors` returns an empty list, tell the user no executors were found and point them at `/build-executor` to scaffold one.

## Step 2: Understand User Intent

Parse `$ARGUMENTS` or the user's natural language request:

| User says | Interpretation |
|-----------|---------------|
| "run ridge" | Select `ml_ridge.py` |
| "all ML models" | Select all `ml_*.py` executors |
| "subgroup analysis with ridge and xgboost" | Select `ml_ridge.py` + `ml_xgboost.py`, grid over subgroups |
| "sweep horizons 1, 5, 25 on lightgbm" | Select `ml_lightgbm.py`, grid: horizon=[1, 5, 25] |

**Flags:**
- `--no-canary` — skip the Step 7b 1-task canary submission. Default behavior is canary-on; only skip when the user has already smoke-tested the pipeline within the last session or is deliberately re-submitting a known-good pipeline.

For multi-executor submissions: submit as **separate array jobs** (independent monitoring and failure handling). Build a dispatch manifest per job.

## Step 3: Build Grid

Before materializing the manifest, check the projected task count:

1. Compute `total_tasks(grid)` from `hpc_mapreduce.job.grid` on the proposed grid.
2. If the count exceeds the cluster's `constraints.max_tasks` advisory (when set) or a common-sense threshold of 1000, surface it and ask: `"This will produce N tasks. Confirm? [y/N]"`.
3. When building the manifest, pass `max_tasks=None` to `build_task_manifest` if the user has confirmed a count above 10_000; otherwise leave the default so `build_task_manifest` raises on accidental explosion.

From executor CLI args and user intent, propose grid dimensions:

```
Running ml_ridge.py and ml_xgboost.py.

Grid parameters (from CLI --help):
  horizon: [1]

Date windows: 10 periods via date_window_shim.py (chunk-id grid dim).

Per executor:
  ml_ridge.py:    10 tasks
  ml_xgboost.py:  10 tasks
  Total: 20 tasks

Adjust grid, or confirm?
```

The user can add dimensions: "also sweep horizon=[1, 5, 25]" → grid becomes 30 tasks per executor (3 horizons × 10 chunk-ids).

### Backtest (date-window) handling

When the selected profile has a `backtest:` block (`start`, `end`, `chunk_duration`, optional `start_arg`/`end_arg`), translate it into a generated shim instead of passing the block into the framework core:

1. Instantiate `templates/date_window_shim.py` (resolve via `_PACKAGE_ROOT / "templates" / "date_window_shim.py"`), filling in the five module-level constants (`START`, `END`, `CHUNK_DUR`, `START_ARG`, `END_ARG`) from the yaml block.
2. Route the shim through the existing Step 6 shim-cache machinery (`shim_cache_key` / `load_cached_shim` / `save_shim`) — reuse that block; do NOT duplicate the code here.
3. Prepend `python3 <materialized_shim_path> --` to the profile's `run` command.
4. Add `chunk-id: [0..N-1]` as a grid dimension where `N` is the period count computed from the `backtest` block (same arithmetic as `date_window_shim._periods()`).
5. Call `build_task_manifest(run, grid, result_dir_template)` — no `backtest=` kwarg. Periods are now simply grid-chunk-id cardinality and are already counted in the grid total.

When the user mentions CLI arguments that the executor doesn't support (e.g., "sweep features=[har, pca]" but `--features` isn't in --help), flag it: "ml_ridge.py doesn't accept --features. Should I add it, or did you mean a different executor?"

## Step 4: Auto-Configure Environment

### Cluster Selection
Ask which cluster to use (present options from `clusters.yaml`). Cache in Claude Code memory.

If `$ARGUMENTS` contains `--cluster <name>`, use that cluster.

Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from cluster config.

### Remote Path
Default: `{cluster.scratch}/{project_dir_name}`
Or use cached value from Claude Code memory.
Confirm with user on first submission.

### Environment Detection
Use `info.imports` from the `ExecutorInfo` captured in Step 1 (fall back to reading the source only if that tuple is empty):

| Imports detected | Classification | Environment |
|-----------------|----------------|-------------|
| `torch`, `tensorflow`, `cuda` | GPU / DL | Load CUDA modules, activate conda env |
| `sklearn`, `xgboost`, `lightgbm` | CPU / ML | Load python modules |
| `numpy`, `pandas` only | CPU / lightweight | Load python modules |

Look up the cluster's available modules from `clusters.yaml`.

For DL executors:
- If cluster has `conda_envs` listed → present options: "Available conda envs on hoffman2: [<your_env>, base]. Which one?"
- If no `conda_envs` in config → ask user: "This executor needs a conda environment with PyTorch. What's the env name on {cluster}?"

Cache environment config in Claude Code memory.

### Resource Estimation

| Executor type | Default resources |
|---------------|-------------------|
| CPU / ML | `cpus: 1, mem: "16G", walltime: "4:00:00"` |
| GPU / DL | `cpus: 4, mem: "16G", walltime: "6:00:00", gpus: 2, gpu_type: <first in cluster gpu_types>` |

Present defaults and let user override: "Resources per task: 1 CPU, 16G, 4h. Adjust?"

### Rsync Excludes
Build exclude list from:
1. `.gitignore` patterns (if file exists)
2. Standard patterns: `__pycache__/`, `*.pyc`, `.git/`, `.claude/`, `.mypy_cache/`, `.hpc_cache/`
3. Result directories (e.g., `results/`)

The `.hpc_cache/` directory holds the content-addressed shim cache (see Step 6). It is local-only — the materialized shim at `src/hpc_chunking_shim.py` is what gets rsynced to the cluster.

## Step 4b: Compute Throughput Plan

After grid expansion produces total_tasks, compute an optimized submission plan:

1. **Load constraints**: `from hpc_mapreduce import ClusterConstraints, parse_constraints` — read constraints from `clusters.yaml` for the selected cluster, then overlay any per-profile constraints from `hpc.yaml` (profile-level fields override cluster-level fields).

2. **Build workload**: `from hpc_mapreduce.job.throughput import WorkloadSpec, compute_submission_plan` — construct a `WorkloadSpec` using `total_tasks` from grid expansion, plus `est_task_duration` if configured in the profile.

3. **Compute plan**: Call `compute_submission_plan(constraints, workload)` to get a `SubmissionPlan` with batched waves.

4. **Display the plan** in the confirmation prompt (Step 5), e.g.:

```
Throughput Plan:
  Strategy:   4 batches (88 tasks each), 2 concurrent, 2 waves, ~30m est.
  Wave 1:     tasks 1-88, 89-176  (submit immediately)
  Wave 2:     tasks 177-264, 265-350  (after wave 1)
```

5. **Embed wave map**: Call `build_wave_map(plan)` to generate a wave-to-task mapping, then call `attach_wave_map(manifest, wave_map)` to embed it in the manifest before writing `_hpc_dispatch.json`. This allows the on-cluster combiner to know which tasks belong to each wave.

If constraints are not configured for the cluster or profile, skip this step and submit as a single array (existing behavior).

## Step 5: Confirm Run Plan

Present the full submission plan:

```
═══════════════════════════════════════════════
  Submission Plan
═══════════════════════════════════════════════

  Cluster:    hoffman2 (SGE)
  Remote:     <remote_path>
  
  Job 1: ml_ridge
    Executor:   python3 src/date_window_shim.py -- python3 src/ml_ridge.py
    Grid:       horizon=[1], chunk-id=[0..9] → 10 grid points
    Date windows: 10 periods via date_window_shim.py (chunk-id grid dim).
    Tasks:      10
    Resources:  1 CPU, 16G, 4:00:00
    Env:        modules=python/3.11.9

  Job 2: ml_xgboost
    Executor:   python3 src/date_window_shim.py -- python3 src/ml_xgboost.py
    Grid:       horizon=[1], chunk-id=[0..9] → 10 grid points
    Date windows: 10 periods via date_window_shim.py (chunk-id grid dim).
    Tasks:      10
    Resources:  1 CPU, 16G, 4:00:00
    Env:        modules=python/3.11.9

  Total tasks: 20

═══════════════════════════════════════════════

Confirm?
```

## Step 6: Generate Dispatch Manifests

For each job, use `hpc_mapreduce.job.grid.build_task_manifest()` to generate a `_hpc_dispatch.json` file locally. This JSON maps each task ID (0-based) to its full command string and result directory.

`_hpc_dispatch.json` is the recoverable artifact for `/monitor` and `/aggregate`; it must persist between waves and across resubmissions (both locally and on the cluster).

For multi-executor submissions, generate one manifest per executor. Name them `_hpc_dispatch_{executor_name}.json` or use separate subdirectories.

### Step 6a: Resume-vs-fresh check (content-hashed manifest)

Before writing the manifest to disk, guard against silently overwriting a prior identical run:

1. Compute the run-level `cmd_sha` from the freshly-built manifest via
   `hpc_mapreduce.job.manifest.aggregate_cmd_sha(manifest)`.
2. Look for an existing content-addressed manifest with the matching prefix
   in the experiment directory:

   ```python
   from hpc_mapreduce.job.manifest import (
       aggregate_cmd_sha,
       find_manifest_by_cmd_sha,
       write_manifest,
       build_manifest_with_resume,
   )

   cmd_sha = aggregate_cmd_sha(manifest)
   prior = find_manifest_by_cmd_sha(experiment_dir, cmd_sha)
   ```

3. If `prior is not None`, **stop and ask the user**:

   ```
   I found an existing run with matching cmd_sha: <prior.name>.
   Resume (re-dispatch only failed tasks) or fresh (new run_id)?
   ```

   - **Resume**: call `/monitor` (or `hpc_mapreduce.report_status_from_manifest`)
     against `prior` to get the list of failing task IDs, then call
     `build_manifest_with_resume(manifest, resume_from=prior, failed_task_ids=[...])`
     which delegates to `hpc_mapreduce.job.resubmit.resubmit_plan` on the
     prior manifest. Submit the returned `ResubmitPlan` via
     `backend.submit_plan(...)` with the failing IDs.
   - **Fresh**: regenerate a new run_id (e.g. incorporate `{date}` /
     `{git_sha}` / a timestamp suffix into `result_dir`), then continue with
     a new `cmd_sha` so the manifest filename is distinct. The prior
     manifest is untouched; retention (default N=10) will age it out over
     time.

4. If `prior is None`, proceed normally.

The Python layer never prompts interactively — this step is the LLM's
responsibility. Once a choice is made, pass it into subsequent calls:

```python
# Fresh path (no prior, or user chose "fresh")
path = write_manifest(experiment_dir, manifest, cmd_sha=cmd_sha)

# Resume path (user chose "resume")
plan = build_manifest_with_resume(
    manifest,
    resume_from=prior,
    failed_task_ids=<from /monitor status>,
    overrides=<optional resource bumps>,
)
# plan is a ResubmitPlan — submit via backend.submit_plan(plan, ...)
```

`write_manifest` also keeps a `manifest.json` symlink/alias pointing at the
most recent file (for back-compat with anything that opens `manifest.json`
directly) and prunes old manifests past `MAX_MANIFESTS` (default 10).

### Interface mismatch → generate a shim

In Step 1, you ran `--help` on each executor. If an executor doesn't accept `--chunk-id`/`--total-chunks` but does accept some form of data slicing (`--start`/`--end`, file lists, date windows), generate a shim.

**Shim template selection.** Pick the starting template based on the kind of parallelism the user wants:

- **Date-window parallelism** (yaml has a `backtest:` block, or the user asked for "split by date range", "6M/1Y periods", etc.) → `templates/date_window_shim.py`. Fill in the `START`, `END`, `CHUNK_DUR`, `START_ARG`, `END_ARG` module-level constants from the yaml block (or the user's described date range + chunk duration).
- **Row-index chunking** (executor accepts `--start`/`--end` as row indices, or the user wants "split by N chunks of data") → `templates/chunking_shim.py`. Fill in `_compute_total_items()` and `translate()`.
- **Anything else** (file lists, GPU device IDs, task-specific seeds, ad-hoc fan-out axes) → start from the blank `templates/shim_template.py` and hand-write the translation.

**Cache-check precondition.** Before generating, check the content-addressed shim cache — re-running for an unchanged executor must produce a byte-identical shim.

```python
from pathlib import Path
from hpc_mapreduce import shim_cache_key, load_cached_shim, save_shim

executor_path = Path("src/<executor>.py")
template_path = Path("<path from _PACKAGE_ROOT / 'templates' / 'chunking_shim.py'>")
cache_dir = Path(".hpc_cache")
materialize_at = Path("src/hpc_chunking_shim.py")  # or "src/hpc_<task>_shim.py"

key = shim_cache_key(executor_path, template_path)
cached = load_cached_shim(cache_dir, key)
```

- If `cached is not None`: copy `cached` to `materialize_at` (overwriting only if the existing file at `materialize_at` starts with `# hpc-shim-key: <key>` — a matching stamp means the on-disk file is a prior cache copy, so it's safe to overwrite). If the existing file has a different stamp or no stamp, treat it as user-edited and do NOT overwrite. Report "shim cache hit: <key>" and skip to the `run:` configuration below.
- If `cached is None`: proceed with generation below. After generating the shim source, call `save_shim(cache_dir, key, shim_source, executor_path=executor_path, template_path=template_path, materialize_at=materialize_at)` to persist it. The helper prepends the `# hpc-shim-key:` stamp automatically.

**Generation (cache miss only).** Read the template at `templates/chunking_shim.py` (resolve path via `python -c 'from hpc_mapreduce import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "templates" / "chunking_shim.py")'`). Fill in:

- `_compute_total_items()` — read the executor source, replicate its data pipeline up to the point where the array length is known
- `translate()` — adjust the return args if the executor uses something other than `--start`/`--end`
- `_CACHE_FILE` — name appropriately or set to `None` to disable

Point the profile's `run` at the shim:

```yaml
run: "python3 src/hpc_chunking_shim.py -- python3 src/executor.py"
chunking:
  total: 100
```

The cache layer guarantees that repeat submissions for the same executor and template reuse the exact same shim bytes, regardless of which Claude session runs `/submit`. A user-edited shim (stamp mismatch) is never overwritten.

Also copy `hpc_mapreduce/map/dispatch.py` to `_hpc_dispatch.py` in the project root.

## Step 7: Sync to Cluster

Push local code + dispatch files to the cluster:

```bash
rsync -az --delete \
    --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='hpc_mapreduce/' \
    # ... add each rsync exclude pattern ...
    . $SSH_TARGET:$REMOTE_PATH/
```

Note: `deploy_runtime()` now also deploys `_hpc_combiner.py` alongside the existing dispatch script.

Verify deployment — existence check:
```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/_hpc_dispatch.json '"$REMOTE_PATH"'/_hpc_dispatch.py'
```

**Verify content, not just existence.** `rsync` exit 0 is necessary but not sufficient: a WSL/DNS hiccup or stale SSH config can cause rsync to silently transfer nothing while still returning success. Before submitting a full array, spot-check the hash of 2–3 files that *should* have just changed (e.g., a source file and the manifest):

```bash
# Local hashes
md5sum _hpc_dispatch.json src/<changed_file>.py
# Remote hashes
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && md5sum _hpc_dispatch.json src/<changed_file>.py'
```

If any hash differs, STOP — re-run rsync with verbose flags (`-avz`) and investigate DNS/ssh-config issues before submitting.

## Step 7b: Canary Submission

**Before submitting the full `-t 1-<total_tasks>` array, submit a 1-task canary** to validate the end-to-end pipeline on the cluster:

```bash
# SGE canary
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && qsub -t 1-1 -N <job_name>_canary -o logs -j y \
    -l <resource_key>=<resource_val> ... \
    -v CONDA_SOURCE=...,CONDA_ENV=...,MODULES=...,EXECUTOR=...,HPC_MANIFEST=...,TOTAL_TASKS=1,TASK_OFFSET=0 \
    <template_path>'

# SLURM canary
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && sbatch --array=1-1 --job-name=<job_name>_canary ... \
    --export=...,TOTAL_TASKS=1,TASK_OFFSET=0 <template_path>'
```

Wait for the canary to reach a terminal state (`sacct -j <jobid>` / `qacct -j <jobid>`), then verify:

1. **Exit code 0** in the log tail (`[dispatch] FAILED` or `ImportError` / `Traceback` = fail).
2. **Expected output artifacts** exist in the task's `result_dir` (whichever of `results.summary_pattern`, `*_reduce.json`, or `metrics_chunk_*.json` the profile declares).
3. **Output is well-shaped** — if prior runs exist, compare CSV header/row count to a known-good file.

**Only if all three pass, proceed to Step 8 (full array submission).** If the canary fails, the fix cost is 1 task; skipping the canary and discovering a bad pipeline after 5000 tasks wastes hours of cluster time and poisons the queue for other users.

To opt out (e.g., already smoke-tested in the last 10 minutes or single-task submission anyway), pass `--no-canary` to `/submit`. Default is canary-on.

## Step 8: Submit

If a throughput plan was computed in Step 4b, use `backend.submit_plan(plan, ...)` instead of `backend.submit_array(...)`. The plan-based submission handles batching tasks into arrays, grouping arrays into waves, and setting up scheduler dependencies between waves (SLURM `--dependency=afterany:...`, SGE `-hold_jid ...`).

If no plan is available (constraints not configured), fall back to the standard single-array submission below.

Determine template from resources (GPU present → `gpu_array`, else `cpu_array`).

Build env vars:
- `EXECUTOR=python3 _hpc_dispatch.py`
- `HPC_MANIFEST=_hpc_dispatch.json`
- `REPO_DIR=<remote_path>`
- `MODULES=<detected modules>`
- `CONDA_SOURCE=<cluster.conda_source>` (if conda env needed)
- `CONDA_ENV=<detected/selected conda_env>` (if needed)
- `TOTAL_TASKS=<total_tasks>`

### SGE Submission

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && qsub \
    -t 1-<total_tasks> \
    -N <job_name> \
    -o logs -j y \
    -l <resource_key>=<resource_val> \
    ... \
    -v CONDA_SOURCE=...,CONDA_ENV=...,MODULES=...,EXECUTOR=...,TOTAL_TASKS=... \
    <template_path>'
```

For GPU jobs: `-l gpu,<gpu_type>,cuda=<count>`.

### SLURM Submission

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && sbatch \
    --array=1-<total_tasks> \
    --job-name=<job_name> \
    --output=logs/%x_%A_%a.out \
    --error=logs/%x_%A_%a.err \
    --mem=<mem> --time=<walltime> --cpus-per-task=<cpus> \
    --export=CONDA_SOURCE=...,CONDA_ENV=...,MODULES=...,EXECUTOR=...,TOTAL_TASKS=... \
    <template_path>'
```

For GPU jobs: `--gres=gpu:<count>` and appropriate partition.

## Step 9: Cache and Report

### Cache decisions
Save to Claude Code memory for this project:
- Executor directory, cluster, remote_path
- Environment: modules, conda_env per executor type (CPU/GPU)
- Default resources, generated shim path

### Report
After submission:
1. Parse the job ID from submission output
2. Report: job ID, executor(s), grid dimensions, total tasks, cluster
3. Suggest running `/monitor` to track progress

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) for >30min | Resource unavailable | Check `sinfo`, try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| ModuleNotFoundError | Env not set up | Check modules and conda_env |
| rsync failure | SSH key issue | Check `ssh $SSH_TARGET hostname` first |
| `--features` not recognized | Executor doesn't support that arg | Check `--help`, update executor |
