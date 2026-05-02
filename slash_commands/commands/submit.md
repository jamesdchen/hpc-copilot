Help me submit HPC jobs via SSH. Discovers experiment executors, builds submission plans conversationally, and handles all deployment.

CLI shapes for every tool referenced below: see `docs/cli-contract.md`.

All cluster commands run remotely via SSH. Code is synced from the local machine before submission.

## Setup

Read cluster definitions:
- `clusters.yaml`: resolve path via `python -c 'from hpc_mapreduce import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Check for existing context (in priority order):

**Migration check (legacy `_hpc_dispatch.json`):** Before any of the
priority checks below, look for a top-level `_hpc_dispatch.json` (or
`manifest.<sha8>.json`, or `manifest.json`) in the experiment dir. These
are artifacts of the pre-`.hpc/tasks.py` model that no longer drive the
framework. If any are present, surface a one-time migration message:

> "I found a legacy dispatch manifest at `_hpc_dispatch.json`. The
> framework no longer reads manifests — task definitions live in
> `.hpc/tasks.py` and per-run state in `.hpc/runs/<run_id>.json`. I'll
> walk you through writing `.hpc/tasks.py` once at Step 6 (using your
> existing manifest as a translation hint if helpful), then we can move
> the old manifest aside. OK to proceed?"

If the user agrees, continue to priority 0 below; the manifest's
existing `tasks[*].cmd` and `tasks[*].params` are useful context for
Step 6's scaffolding conversation but are not consumed by the framework.
Once the new `.hpc/tasks.py` is committed, suggest the user `git mv
_hpc_dispatch.json .hpc/legacy/` (or simply delete it). Don't proceed
silently — a stale `_hpc_dispatch.json` next to a fresh `.hpc/tasks.py`
is confusing on inspection.

0. **In-flight run journal**: The per-run journal lives at `~/.claude/hpc/<repo_hash>/runs/<run_id>.json`. Call `slash_commands.session.find_in_flight_runs(cwd)`. If any in-flight run is found, offer: "Found in-flight run [{profile} on {cluster}, jobs {job_ids}, last status {complete}/{total} @ {age}]. Resume monitoring with /monitor-hpc, or start a new submission?"
   - This only handles the case where the user wants to switch context away from a fresh `/submit` toward picking up an existing run; otherwise fall through to priority 1.
   - **Group by `campaign_id` when displaying multiple in-flight runs.** Each `RunRecord` carries a `campaign_id` field (empty string for open-loop submits). When the user has more than ~3 in-flight runs and at least one has a non-empty `campaign_id`, render the offer grouped: "Found 3 in-flight campaigns and 2 standalone runs: campaign `ml_ridge_q1` (4 iterations in flight, last completed iteration's loss=0.42), campaign `walk_forward_2026q1` (1 iteration in flight), …, plus 2 standalone runs (`<run_id_1>`, `<run_id_2>`). Resume one with /monitor-hpc / /campaign status, or start a new submission?" The flat list is fine for ≤3 runs.

1. **Previous run**: If `.hpc/tasks.py` exists, the experiment has already been scaffolded. List the per-run sidecars under `.hpc/runs/` (newest-first via `find_existing_runs(experiment_dir)` from `hpc_mapreduce`) and offer: "Previous run: [run_id, profile, tasks, cluster, age]. Resubmit same, modify (edit `.hpc/tasks.py`), or start fresh?"
   - **Resubmit same** → reuse the existing `.hpc/tasks.py`, recompute `cmd_sha` (it'll match because `tasks.py` is unchanged), skip to Step 5 (sync + submit). The new sidecar's `run_id` differs but `cmd_sha` matches the prior run.
   - **Modify** → tell the user to edit `.hpc/tasks.py` directly (`_TASKS = [...]`), commit the change, and re-run `/submit`. The new `cmd_sha` will be different, so it's a fresh run.
   - **Start fresh** → only reachable when the user wants a clean reset; offer to delete `.hpc/tasks.py` so the scaffolding flow at Step 6 fires again.

2. **No tasks.py yet**: Continue to Step 1 (full discovery).

   When prompting the user about reuse vs. fresh, list the distinct `(profile, cluster)` pairs from recent run sidecars (via `find_existing_runs(experiment_dir)`) so they can pick "same as last `ml_ridge` submission" without re-answering interview questions. Each sidecar carries the full v2 config snapshot — resources, env, constraints, runtime — so reuse is a one-line copy from the matching sidecar.

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

Present the inventory (use `info.name` and `info.path` as the identifiers; `info.docstring` is handy for the one-line summary). Examples are illustrative — `/submit` works with any executor that accepts grid-shaped CLI flags.

```
Executors found in src/ (illustrative — names, flags, and domain are per-experiment):
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
| "sweep horizons 1, 5, 25 on lightgbm" | Select `ml_lightgbm.py`, fan out over `horizon ∈ [1, 5, 25]` (3 tasks) |

**Flags:**
- `--no-canary` — skip the Step 7b 1-task canary submission. Default behavior is canary-on; only skip when the user has already smoke-tested the pipeline within the last session or is deliberately re-submitting a known-good pipeline.

For multi-executor submissions: submit as **separate array jobs** (independent monitoring and failure handling). Each gets its own `run_id` and per-run sidecar at `.hpc/runs/<run_id>.json`; the same `.hpc/tasks.py` is reused if the parallelization axis matches, otherwise the agent writes a new one (the file is the single seam between executors and the framework).

## Step 3: Plan the parallelization axis

In the new model, the **task list lives in user-written `.hpc/tasks.py`**: a small Python module exposing `total()` and `resolve(task_id)`. Step 6 walks the user through writing it once per experiment, adapting from the canonical example at `hpc_mapreduce/templates/tasks_example.py`. From then on, the file is committed to git and reused on every submit.

Step 3's job is to gather enough context that Step 6 can write a sensible first draft. From executor CLI args and the user's intent, propose:

- **The shape of the axis**: Cartesian product over named hyperparameters? Chunking by row count? Date-window backtest? Something else?
- **The kwargs `resolve(task_id)` should return**: e.g. `{"seed": ..., "model": ...}` for a grid; `{"chunk_id": ..., "total_chunks": ...}` for chunking; `{"window_start": ..., "window_end": ...}` for backtests.
- **The expected task count** so we can sanity-check before writing.

Present a draft outline:

```
Running ml_ridge.py and ml_xgboost.py.

Proposed parallelization (one tasks.py per executor):
  Axis:        Cartesian product over horizon + chunk-id
  Kwargs:      {"horizon": int, "chunk_id": int, "total_chunks": int}
  Cardinality: 3 horizons × 10 chunks = 30 tasks per executor
  Total:       60 tasks

Adjust the axis, or confirm?
```

If the projected task count (per executor or overall) exceeds the cluster's `constraints.max_tasks` advisory (when set) or a common-sense threshold of ~1000, surface it explicitly: `"This will produce N tasks. Confirm? [y/N]"`. The actual cardinality is whatever `tasks.total()` returns once `.hpc/tasks.py` is written — Step 6 verifies it matches the user's intent before submission.

When the user mentions CLI arguments that the executor doesn't support (e.g., "sweep features=[har, pca]" but `--features` isn't in `--help`), flag it: `"ml_ridge.py doesn't accept --features. Should I add it, or did you mean a different executor?"`.

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
2. Standard patterns: `__pycache__/`, `*.pyc`, `.git/`, `.claude/`, `.mypy_cache/`
3. Result directories (e.g., `results/`)

The local `.hpc/` directory **does** ride rsync (so the cluster receives `tasks.py` and the in-flight `runs/<run_id>.json` sidecar). Don't add `.hpc/` to the exclude list. The framework files inside the cluster-side `.hpc/` (`_hpc_dispatch.py`, `_hpc_combiner.py`, `templates/`) are placed there separately by `deploy_runtime` and are protected from rsync `--delete` via `DEFAULT_RSYNC_EXCLUDES` in `hpc_mapreduce.infra.remote`.

## Step 4b: Compute Throughput Plan

After grid expansion produces total_tasks, compute an optimized submission plan:

1. **Load constraints**: `from hpc_mapreduce import ClusterConstraints, parse_constraints` — read constraints from `clusters.yaml` for the selected cluster, then overlay any per-profile constraints the user supplied in this submit interview (the resolved overrides will be persisted to the run sidecar's `constraints` field).

2. **Build workload**: `from hpc_mapreduce.job.throughput import WorkloadSpec, compute_submission_plan` — construct a `WorkloadSpec` using `total_tasks` from grid expansion, plus `est_task_duration` if configured in the profile.

3. **Compute plan**: Call `compute_submission_plan(constraints, workload)` to get a `SubmissionPlan` with batched waves.

4. **Display the plan** in the confirmation prompt (Step 5), e.g.:

```
Throughput Plan:
  Strategy:   4 batches (88 tasks each), 2 concurrent, 2 waves, ~30m est.
  Wave 1:     tasks 1-88, 89-176  (submit immediately)
  Wave 2:     tasks 177-264, 265-350  (after wave 1)
```

5. **Embed wave map**: Call `build_wave_map(plan)` to generate a wave-to-task mapping. The map is then passed into `write_run_sidecar(..., wave_map=wave_map)` at Step 6d so it lives in `.hpc/runs/<run_id>.json`. The cluster-side combiner reads it from there to know which tasks belong to each wave.

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
    Executor:    python3 src/ml_ridge.py
    tasks.py:    .hpc/tasks.py — kwargs = {horizon, window_start, window_end} per task
    Cardinality: 1 horizon × 10 date windows = 10 tasks
    Resources:   1 CPU, 16G, 4:00:00
    Env:         modules=python/3.11.9

  Job 2: ml_xgboost
    Executor:    python3 src/ml_xgboost.py
    tasks.py:    .hpc/tasks.py — kwargs = {horizon, window_start, window_end} per task
    Cardinality: 1 horizon × 10 date windows = 10 tasks
    Resources:   1 CPU, 16G, 4:00:00
    Env:         modules=python/3.11.9

  Total tasks: 20

═══════════════════════════════════════════════

Confirm?
```

## Step 6: Scaffold (or reuse) `.hpc/tasks.py` and write the per-run sidecar

This is the **central agent-driven moment** that makes claude-hpc different from a generic mapreduce library. Instead of the framework guessing parallelization axes from a YAML schema, the LLM walks the user through writing a small `total()` / `resolve(task_id)` module **once per experiment**, then commits it. From then on, every submission reuses it byte-for-byte.

### Step 6a: Reuse if `.hpc/tasks.py` exists

```python
from pathlib import Path
from hpc_mapreduce import (
    framework_subdir, tasks_path, load_tasks_module, compute_cmd_sha,
)

experiment_dir = Path.cwd()
framework_subdir(experiment_dir)        # mkdir .hpc/, write .hpc/.gitignore
tp = tasks_path(experiment_dir)         # .hpc/tasks.py
```

If `tp.exists()`, the experiment was already scaffolded. **Read it as-is**, never regenerate:

```python
tasks = load_tasks_module(tp)
n = tasks.total()
sample = tasks.resolve(0)               # sanity-check signature
print(f"reusing existing .hpc/tasks.py: total()={n}, resolve(0)={sample}")
```

If the user wants to change the axis, tell them to edit `.hpc/tasks.py` directly and re-run `/submit`. The framework never overwrites a user-authored file. Skip to Step 6c.

### Step 6b: Scaffold from the canonical example (first submit only)

If `tp.exists()` is False, enter the scaffolding sub-flow:

1. **Read the canonical example.** Resolve `hpc_mapreduce/templates/tasks_example.py` via `_PACKAGE_ROOT / "templates" / "tasks_example.py"` and read it. This is the only `tasks.py` reference the framework ships — eager-materialized `_TASKS = [...]`, with three commented-out usage patterns inline (Cartesian product, chunking by row count, date-window backtest).

2. **Gather context for the draft.** Read the user's executor module(s) (the same `info.path` from Step 1's `discover_executors`) and any `meta.json` at the experiment root for axis hints (parameter names, ranges, chunking intent, date windows). Recent run sidecars under `.hpc/runs/` are also a useful source — they capture the full kwargs dict from any previous `tasks.resolve(i)` materializations.

3. **Walk the user through writing the file.** This is conversational, not template substitution. The agent:
   - Re-states the axis from Step 3 in concrete terms (e.g. "We're going to materialize a list of {seed, model} dicts, one per task — 4 tasks total. Sound right?").
   - Drafts a minimal `_TASKS` for that axis and shows it to the user.
   - Lets the user paste a snippet, describe in prose, or point at existing code; the agent translates that into `_TASKS`, `total()`, `resolve(task_id)`.
   - Iterates. The user is the source of truth on what the axis means.

   Eager memoization is the **convention, not a choice**: `_TASKS` is materialized at module load, `total()` returns `len(_TASKS)`, `resolve(i)` returns `_TASKS[i]`. This gives free `cmd_sha`, submit-time error catching, and laptop-inspectability. Lazy variants are not encouraged.

4. **Write the file and commit it.**

   ```python
   tp.write_text(final_source)          # the full tasks.py text
   import subprocess
   subprocess.run(["git", "add", str(tp)], check=True)
   subprocess.run(
       ["git", "commit", "-m", f"Add .hpc/tasks.py for {executor_name}"],
       check=True,
   )
   ```

   Print the commit SHA. **No push** — the user controls when their work goes upstream. If the working tree is detached or the directory is not a git repo, warn the user and continue (the file still gets written; commit is best-effort). Subsequent submits hit Step 6a and skip this entire sub-flow.

### Step 6c: Compute `cmd_sha` and check for resume

The materialized task list is the source of identity for the run:

```python
from hpc_mapreduce import (
    compute_cmd_sha, compute_tasks_py_sha, find_run_by_cmd_sha,
    write_run_sidecar, runs_subdir,
)
from datetime import datetime, timezone
import subprocess

tasks = load_tasks_module(tp)
cmd_sha = compute_cmd_sha(tasks)        # SHA-256 over normalized resolve(i) dicts
tasks_py_sha = compute_tasks_py_sha(tp)
prior = find_run_by_cmd_sha(experiment_dir, cmd_sha)
```

If `prior is not None`, **stop and ask the user**:

```
I found a prior run with the same cmd_sha: <prior.stem>.
Resume (re-dispatch only failed tasks) or fresh (new run_id)?
```

- **Resume**: call `/monitor-hpc --run-id <prior.stem>` (or `report_status` directly) to enumerate failing task IDs, then build a `ResubmitPlan` via `resubmit_plan(task_count=tasks.total(), failed_task_ids=[...])` and submit via `backend.submit_plan(plan, ...)`. The new sidecar (written below) carries the same `cmd_sha` but a fresh `run_id` — both runs share provenance via the SHA.
- **Fresh**: ask the user how they want the new run distinguished (e.g. a different result_dir suffix, a profile name change, or simply accept that the new sidecar is a deliberate rerun). The new `cmd_sha` will only differ if `tasks.py` itself changes.

### Step 6d: Compute the throughput plan and write the sidecar

With `total = tasks.total()` known, run Step 4b's throughput planner (already covered above) to get `wave_map`. Then write the per-run sidecar — this is the audit-trail artifact `/monitor-hpc` and `/aggregate` read on the cluster:

```python
run_id = f"{profile}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{cmd_sha[:8]}"
git_sha = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"],
    capture_output=True, text=True,
).stdout.strip() or "nogit"

sidecar_path = write_run_sidecar(
    experiment_dir,
    run_id=run_id,
    cmd_sha=cmd_sha,
    claude_hpc_version=__import__("hpc_mapreduce").__version__,
    submitted_at=datetime.now(timezone.utc).isoformat(),
    executor=run_cmd,                            # full shell cmd, e.g. "python3 scripts/train.py"
    result_dir_template=result_dir_template,     # e.g. "results/{git_sha}/task_{task_id}"
    task_count=tasks.total(),
    tasks_py_sha=tasks_py_sha,
    wave_map=wave_map,                           # from Step 4b's build_wave_map(plan)
    extra={"git_sha": git_sha},
    # ----- v2 config snapshot — populate everything that applies -----
    cluster=cluster_name,                        # e.g. "hoffman2" / "discovery"
    profile=profile,                             # the label distinguishing this submission shape
    project=project,                             # short project name from the interview
    remote_path=remote_path,
    resources=resources,                         # {"cpus": 8, "mem": "64G", "walltime": "...", ...}
    env=env,                                     # {"modules": "...", "conda_env": "..."}
    env_group=env_group,                         # clusters.yaml env_group key, if used
    constraints=resolved_constraints,            # the per-experiment overlay on clusters.yaml
    gpu_fallback=gpu_fallback,                   # ordered GPU types if applicable
    max_retries=max_retries,
    runtime=runtime,                             # "uv" if requested
    auto_retry=auto_retry,                       # per-category override; None = use defaults
    aggregate_defaults=aggregate_defaults,       # {"require_outputs": "...", "expect_output": "...", "aggregate_cmd": "..."}
)
```

Pass `None` (or omit) for any v2 field that doesn't apply — they're all optional and absent keys are stripped from the on-disk JSON. Subsequent `/aggregate` and `/monitor-hpc` invocations read these fields back so the user never has to re-answer the interview.

For multi-executor submissions, write one sidecar per executor — `run_id` and `executor` differ, but `tasks.py` is per-experiment and may be shared if the axes match.

`write_run_sidecar` automatically prunes old sidecars past `MAX_RUNS` (default 500; override via `HPC_MAX_RUNS`). Identity is the `run_id`, addressable directly at `.hpc/runs/<run_id>.json`.

## Step 7: Sync to Cluster

Two pipes populate the cluster's `$REMOTE_PATH`. **Don't hand-copy any framework files** — `deploy_runtime` does that via scp, and rsync would otherwise overwrite the cluster-side `.hpc/_hpc_dispatch.py` etc. with files that don't exist locally.

1. **`rsync_push`** ships your code plus the local `.hpc/` (which contains only `tasks.py` and `runs/<run_id>.json` — no framework files):

   ```bash
   rsync -az --delete \
       --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
       --exclude='hpc_mapreduce/' \
       --exclude='.hpc/_hpc_dispatch.py' \
       --exclude='.hpc/_hpc_combiner.py' \
       --exclude='.hpc/templates/' \
       # ... plus any project-specific excludes ...
       . $SSH_TARGET:$REMOTE_PATH/
   ```

   The `.hpc/_hpc_*.py` and `.hpc/templates/` excludes prevent `--delete` from wiping the framework files that `deploy_runtime` placed on the cluster. `DEFAULT_RSYNC_EXCLUDES` in `hpc_mapreduce.infra.remote` has these baked in; if you call `rsync_push` directly, you get them for free.

2. **`deploy_runtime`** scp's the framework files into `{remote_path}/.hpc/`:
   - `_hpc_dispatch.py` (the framework executor)
   - `_hpc_combiner.py`
   - `templates/{cpu_array,gpu_array}.{sh,slurm}`
   - and the importable stubs `hpc_mapreduce/map/{context,metrics_io}.py` (these go to `{remote_path}/hpc_mapreduce/map/`, not `.hpc/`)

   ```python
   from hpc_mapreduce import deploy_runtime
   deploy_runtime(host=cluster.host, user=cluster.user, remote_path=remote_path)
   ```

   Run **after** `rsync_push` (rsync's `--delete` would otherwise blow away the freshly-scp'd files; the excludes above protect them, but ordering remains important on every submit).

Verify deployment — existence check (paths are now under `.hpc/`):
```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/.hpc/tasks.py '"$REMOTE_PATH"'/.hpc/runs/<run_id>.json '"$REMOTE_PATH"'/.hpc/_hpc_dispatch.py'
```

**Verify content, not just existence.** `rsync` exit 0 is necessary but not sufficient: a WSL/DNS hiccup or stale SSH config can cause rsync to silently transfer nothing while still returning success. Before submitting a full array, spot-check the hash of 2–3 files that *should* have just changed (e.g., a source file and `tasks.py`):

```bash
# Local hashes
md5sum .hpc/tasks.py src/<changed_file>.py
# Remote hashes
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && md5sum .hpc/tasks.py src/<changed_file>.py'
```

If any hash differs, STOP — re-run rsync with verbose flags (`-avz`) and investigate DNS/ssh-config issues before submitting.

## Step 7b: Canary Submission

**Before submitting the full `-t 1-<total_tasks>` array, submit a 1-task canary** to validate the end-to-end pipeline on the cluster:

```bash
# SGE canary
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && qsub -t 1-1 -N <job_name>_canary -o logs -j y \
    -l <resource_key>=<resource_val> ... \
    -v CONDA_SOURCE=...,CONDA_ENV=...,MODULES=...,EXECUTOR=python3 .hpc/_hpc_dispatch.py,HPC_RUN_ID=<run_id>,HPC_CMD_SHA=<cmd_sha>,HPC_TASK_COUNT=<n>,TASK_OFFSET=0 \
    .hpc/templates/<template>'

# SLURM canary
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && sbatch --array=1-1 --job-name=<job_name>_canary ... \
    --export=EXECUTOR=python3 .hpc/_hpc_dispatch.py,HPC_RUN_ID=<run_id>,HPC_CMD_SHA=<cmd_sha>,HPC_TASK_COUNT=<n>,TASK_OFFSET=0 \
    .hpc/templates/<template>'
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

Determine template from resources (GPU present → `gpu_array`, else `cpu_array`). The template path is **cluster-relative**: `.hpc/templates/cpu_array.sh` (SGE) or `.hpc/templates/cpu_array.slurm` (SLURM); `deploy_runtime` placed it there. Don't pass an absolute or local path.

Build env vars:
- `EXECUTOR=python3 .hpc/_hpc_dispatch.py` — the framework executor scp'd by `deploy_runtime`
- `HPC_RUN_ID=<run_id>` — locates the per-run sidecar at `.hpc/runs/<run_id>.json`
- `HPC_CMD_SHA=<cmd_sha>` — hash of the materialized `tasks.py` task list (provenance only)
- `HPC_TASK_COUNT=<task_count>` — scheduler array length, equal to `tasks.total()`
- `REPO_DIR=<remote_path>`
- `MODULES=<detected modules>`
- `CONDA_SOURCE=<cluster.conda_source>` (if conda env needed)
- `CONDA_ENV=<detected/selected conda_env>` (if needed)

The cluster-side template translates the scheduler's per-task index (`SGE_TASK_ID` / `SLURM_ARRAY_TASK_ID`) into `HPC_TASK_ID` (0-based) before exec'ing `$EXECUTOR`, which then imports `.hpc/tasks.py`, calls `tasks.resolve(HPC_TASK_ID)`, and runs the executor command from the sidecar with kwargs merged into the env.

**Building the job_env (with runtime support)**

When the user's spec carries `runtime: "uv"`, `HPC_RUNTIME=uv` MUST be in the job's env so the cluster-side template's `uv sync` preamble fires. The `build_job_env` helper takes a runtime-tagged dict and threads it through:

```python
from slash_commands.runner import build_job_env
base_env = {
    "EXECUTOR": "python3 .hpc/_hpc_dispatch.py",
    "HPC_RUN_ID": run_id,
    "HPC_CMD_SHA": cmd_sha,
    "HPC_TASK_COUNT": str(tasks.total()),
    "REPO_DIR": remote_path,
    "MODULES": ...,
    "CONDA_SOURCE": ...,
    "CONDA_ENV": ...,
}
# Pass {"runtime": "uv"} to enable HPC_RUNTIME forwarding; an empty dict
# leaves base_env untouched.
job_env = build_job_env({"runtime": runtime} if runtime else {}, base_env)
```

> **NOTE:** when using `SGEBackend(pass_env_keys=...)`, the tuple MUST include `"HPC_RUN_ID"`, `"HPC_CMD_SHA"`, `"HPC_TASK_COUNT"`, and (if applicable) `"HPC_RUNTIME"` so the qsub `-v` filter forwards them. The SLURM backend forwards everything in `job_env` automatically.

### SGE Submission

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && qsub \
    -t 1-<task_count> \
    -N <job_name> \
    -o logs -j y \
    -l <resource_key>=<resource_val> \
    ... \
    -v CONDA_SOURCE=...,CONDA_ENV=...,MODULES=...,EXECUTOR=python3 .hpc/_hpc_dispatch.py,HPC_RUN_ID=<run_id>,HPC_CMD_SHA=<cmd_sha>,HPC_TASK_COUNT=<n> \
    .hpc/templates/cpu_array.sh'
```

For GPU jobs: `-l gpu,<gpu_type>,cuda=<count>`, and use `.hpc/templates/gpu_array.sh`.

### SLURM Submission

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && sbatch \
    --array=1-<task_count> \
    --job-name=<job_name> \
    --output=logs/%x_%A_%a.out \
    --error=logs/%x_%A_%a.err \
    --mem=<mem> --time=<walltime> --cpus-per-task=<cpus> \
    --export=CONDA_SOURCE=...,CONDA_ENV=...,MODULES=...,EXECUTOR=python3 .hpc/_hpc_dispatch.py,HPC_RUN_ID=<run_id>,HPC_CMD_SHA=<cmd_sha>,HPC_TASK_COUNT=<n> \
    .hpc/templates/cpu_array.slurm'
```

For GPU jobs: `--gres=gpu:<count>`, appropriate partition, and use `.hpc/templates/gpu_array.slurm`.

## Step 8b: Verify the array is actually queued/running

`qsub`/`sbatch` returning a job ID is necessary but not sufficient — the scheduler can still place the array into an error state (`Eqw` on SGE, `BOOT_FAIL`/`NODE_FAIL` on SLURM) or, on a wedged controller, drop the registration entirely. Confirm each returned job ID is alive on the cluster **before** reporting success (Step 9) or writing the journal record (Step 10). A poisoned run that lands in the journal is worse than a clean failure here, because `/monitor-hpc` will keep latching onto a dead job ID.

Query the scheduler for every job ID returned by `backend.submit_array` / `backend.submit_plan`:

```bash
# SLURM
ssh $SSH_TARGET 'squeue -j '"$JOB_IDS"' -h -o "%i %T %r"; \
                 sacct -j '"$JOB_IDS"' -n -P -o JobID,State,Reason 2>&1 | head'

# SGE — qstat -j prints the queue-instance reason if the job is in error
ssh $SSH_TARGET 'qstat -j '"$JOB_IDS"' 2>&1 | head -40; \
                 qstat -u '"$USER"' | awk "NR>2"'
```

`$JOB_IDS` is comma-separated for SLURM (`12345,12346`) and space-separated for SGE.

**Healthy** (proceed): `PENDING` / `RUNNING` / `CONFIGURING` / `COMPLETING` (SLURM); `qw` / `hqw` / `r` / `t` / `Rq` / `Rr` (SGE). Wave-2+ jobs from a plan-based submission are *expected* to be `PENDING` with `Reason=Dependency` (SLURM) or `hqw` (SGE) — that is healthy, not a failure.

**Failed** (abort — do NOT call `submit_and_record`):
- SLURM state in `{BOOT_FAIL, FAILED, NODE_FAIL, OUT_OF_MEMORY, TIMEOUT, DEADLINE, REVOKED, SPECIAL_EXIT}`, or `CANCELLED` within seconds of submit
- SGE state starting with `E` (e.g. `Eqw`) or `d` (deletion in progress)
- Job ID absent from both `squeue`/`qstat` and `sacct`/`qacct` after one retry (~3s pause): the scheduler never registered it

If the first query shows an ID as unknown, retry **once** after a brief pause (busy SLURM controllers can lag a second or two before `squeue` reflects a new submission). If still unknown, treat as failed.

On failure: surface the scheduler's reason verbatim (`qstat -j <id>` line `error reason 1:` for SGE, `sacct -j <id> -o JobID,State,Reason` for SLURM), tell the user which job ID is bad, and stop. Do not run Step 9 or Step 10 — the partial state is recoverable only if nothing was journaled. The user can then either fix the underlying issue (resources, queue, env) and re-run `/submit`, or, for SGE-specific transient `Eqw`, run `qmod -cj <jobid>` and re-verify.

If the canary in Step 7b just succeeded, this verification almost always passes; the value is catching the rare case where the full-array submit hits a quota/AR/queue limit the canary did not.

## Step 9: Cache and Report

### Cache decisions
Save to Claude Code memory for this project:
- Executor directory, cluster, remote_path
- Environment: modules, conda_env per executor type (CPU/GPU)
- Default resources

### Report
After submission **and the Step 8b verification**:
1. Parse the job ID from submission output
2. Report: job ID, executor(s), grid dimensions, total tasks, cluster, and the verified scheduler state (e.g. "all 4 array jobs PENDING/RUNNING")
3. Suggest running `/monitor-hpc` to track progress

## Step 10: Record the submission in the run journal

After the per-run sidecar is written locally and rsync'd to the cluster, persist the bootstrap context for cold-session resume:

```python
from pathlib import Path
from slash_commands import runner

record, deduped = runner.submit_and_record(
    Path.cwd(),
    profile=<job_name>,
    cluster=<cluster_name>,
    ssh_target=f"{cluster.user}@{cluster.host}",
    remote_path=<remote_path>,
    job_name=<job_name>,
    run_id=<run_id from Step 6d, e.g. "ml_ridge-20260429-153012-abc12345">,
    job_ids=<list of job IDs returned by backend.submit_plan>,
    total_tasks=tasks.total(),
)
# `deduped == True` means a journal record for this run_id already existed
# and the call was a no-op replay. When that happens, do NOT call
# backend.submit_plan again — the original cluster jobs are already running.
# Just resume monitoring.
```

`submit_and_record` is keyed on `run_id` (the timestamp + cmd_sha8 string from Step 6d); a retry with the same run_id deduplicates without re-submitting. The journal entry lets a future `/monitor-hpc` (no args) auto-discover this run and resume monitoring with one keystroke instead of re-asking for cluster / job_ids / etc.

Slash commands MUST call `slash_commands.runner.submit_and_record` rather than writing to `slash_commands.session` directly — the bundled helper guards the journal write under a flock and keeps the run record consistent.

For multi-executor submissions (one sidecar per executor), call `submit_and_record` once per submitted job.

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
