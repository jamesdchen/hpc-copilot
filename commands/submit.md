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

2. **hpc.yaml exists**: Read it as optional context. If it has `profiles`, offer: "I see profiles: [list]. Use one, or build a new submission?" If using a profile, extract its `run`, `grid`, `backtest`, `constraints`, `env_group`, and `resources` as defaults and skip to Step 3 for confirmation.

3. **Neither exists**: Continue to Step 1 (full discovery).

## SSH Quoting

Single-quote the remote command so variables expand on the cluster, not locally:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && echo $SGE_TASK_ID'
```

## Step 1: Discover Executors

Ask the user which directory contains their experiment executors. (Cache in Claude Code memory for this project after first ask.)

Read all `.py` files in that directory. Classify each:

| Category | Detection | Examples |
|----------|-----------|---------|
| **Executor** | Has `argparse` + `if __name__ == "__main__"` + does computation | `ml_ridge.py`, `dl_patchts.py` |
| **Shared utility** | No `if __name__` block, only function/class defs | `loading.py`, `transforms.py` |

For each executor, run `python3 <script> --help` to map its CLI interface. Parse:
- Grid-able parameters (model hyperparams, feature types, etc.)
- Data arguments (`--data-path`, `--horizon`, `--start`, `--end`)
- Output arguments (`--output-file`)

Present the inventory:

```
Executors found in src/:
  ml_ridge.py     — args: --horizon, --data-path, --train-window, --start, --end, --output-file
  ml_xgboost.py   — args: --horizon, --data-path, --train-window, --start, --end, --output-file
  dl_patchts.py   — args: --horizon, --data-path, --gpu-count, --start, --end, --output-file

Which do you want to run?
```

## Step 2: Understand User Intent

Parse `$ARGUMENTS` or the user's natural language request:

| User says | Interpretation |
|-----------|---------------|
| "run ridge" | Select `ml_ridge.py` |
| "all ML models" | Select all `ml_*.py` executors |
| "subgroup analysis with ridge and xgboost" | Select `ml_ridge.py` + `ml_xgboost.py`, grid over subgroups |
| "sweep horizons 1, 5, 25 on lightgbm" | Select `ml_lightgbm.py`, grid: horizon=[1, 5, 25] |

For multi-executor submissions: submit as **separate array jobs** (independent monitoring and failure handling). Build a dispatch manifest per job.

## Step 3: Build Grid

Before materializing the manifest, check the projected task count:

1. Compute `total_tasks(grid, backtest)` from `hpc_mapreduce.job.grid` on the proposed grid.
2. If the count exceeds the cluster's `constraints.max_tasks` advisory (when set) or a common-sense threshold of 1000, surface it and ask: `"This will produce N tasks. Confirm? [y/N]"`.
3. When building the manifest, pass `max_tasks=None` to `build_task_manifest` if the user has confirmed a count above 10_000; otherwise leave the default so `build_task_manifest` raises on accidental explosion.

From executor CLI args and user intent, propose grid dimensions:

```
Running ml_ridge.py and ml_xgboost.py.

Grid parameters (from CLI --help):
  horizon: [1]

Backtest: 2020-01-01 to 2024-12-31 (6M periods) → 10 periods

Per executor:
  ml_ridge.py:    1 grid point × 10 periods = 10 tasks
  ml_xgboost.py:  1 grid point × 10 periods = 10 tasks
  Total: 20 tasks

Adjust grid, backtest, or confirm?
```

The user can add dimensions: "also sweep horizon=[1, 5, 25]" → grid becomes 3 points × 10 periods = 30 per executor.

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
Read executor source code for import statements:

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
    Executor:   python3 src/ml_ridge.py
    Grid:       horizon=[1] → 1 grid point
    Backtest:   2020-01-01 to 2024-12-31 (6M) → 10 periods
    Tasks:      1 × 10 = 10
    Resources:  1 CPU, 16G, 4:00:00
    Env:        modules=python/3.11.9

  Job 2: ml_xgboost
    Executor:   python3 src/ml_xgboost.py
    Grid:       horizon=[1] → 1 grid point
    Backtest:   2020-01-01 to 2024-12-31 (6M) → 10 periods
    Tasks:      1 × 10 = 10
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

Verify deployment:
```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/_hpc_dispatch.json '"$REMOTE_PATH"'/_hpc_dispatch.py'
```

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
- Default resources, backtest config

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
