# hpc.yaml Specification

**This file is optional.** Claude can build submission plans conversationally without it. Use `hpc.yaml` when you want to pre-configure profiles as reusable shortcuts, or when you prefer declarative config over conversational setup.

When `hpc.yaml` exists, `/submit` reads it as pre-populated context — offering existing profiles alongside the option to build a new submission from scratch.

## Top-Level Fields

These are shared across all profiles:

| Field | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | Short project name (used in job names, paths, logs) |
| `cluster` | string | yes | Cluster key matching an entry in `clusters.yaml` |
| `remote_path` | string | yes | Absolute path on the remote cluster |
| `rsync_exclude` | list[str] | no | Patterns passed to `rsync --exclude` during sync |
| `experiment_paths` | list[str] | no | Glob patterns for experiment YAML configs |
| `cluster_envs` | map | no | Per-cluster env overrides keyed by cluster name, then env_group name |

## profiles

A map of **profile_name -> profile_config**. Each profile is either:
- A **single-stage profile** with `run`, `grid`, `resources`, etc. at the profile level
- A **multi-stage profile** with a `stages` key containing a DAG of stages

When `profiles` is present, `run`/`grid`/`resources` are NOT at the top level.

Top-level `project`, `cluster`, `remote_path`, `rsync_exclude` are shared across all profiles.

### Single-Stage Profile Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `run` | string | yes | Shell command (the executor invocation). Per-task kwargs are exported as env vars (uppercased + `HPC_KW_*`) by the cluster-side dispatcher; the executor reads them however it wants. |
| `resources` | map | yes | Resource request per task |
| `env` | map | no | Environment setup (modules, conda_env) |
| `env_group` | string | no | Key into `cluster_envs[cluster]` for env overrides |
| `results` | map | no | Result collection config |
| `constraints` | map | no | Cluster constraints for throughput optimization |
| `gpu_fallback` | list[str] | no | Ordered GPU types to try |
| `max_retries` | int | no | Max auto-resubmissions on failure |
| `auto_retry` | map | no | Per-category retry policy honored by `hpc-mapreduce failures`. See *auto_retry* below. |
| `runtime` | string | no | Runtime profile for cluster-side execution. `"uv"` prefixes every task command with `uv run` and triggers a `uv sync` preamble in the job template (gated on `HPC_RUNTIME=uv`). The cluster must have `uv` on PATH; the template fails fast (exit 2) if not. Default: bare `python`. |

**Parallelization axis lives in `.hpc/tasks.py`.** The number of
tasks and the per-task kwargs are determined by the user-written
`total()` and `resolve(task_id)` callables there — written in plain
Python (e.g. `itertools.product`, slicing, date-window
comprehensions). See `hpc_mapreduce/templates/tasks_example.py` for
the canonical reference.

### Multi-Stage Profiles

When a profile contains a `stages` key, each stage has the same fields as a single-stage profile, plus:

| Field | Type | Required | Description |
|---|---|---|---|
| `depends_on` | string or list[str] | no | Stage(s) that must complete first |

A stage's parallelism is determined by its `.hpc/tasks.py`: if `tasks.total() == 1`, the stage runs as a single job; if `> 1`, the stage fans out (parallel tasks) and then fans in via `results.aggregate_cmd`.

## env

| Field | Type | Required | Description |
|---|---|---|---|
| `modules` | string | no | Space-separated modules to load |
| `conda_env` | string | no | Conda environment to activate |

## resources

| Field | Type | Required | Description |
|---|---|---|---|
| `cpus` | int | no | CPU cores per task |
| `mem` | string | yes | Memory per task (e.g., `"16G"`) |
| `walltime` | string | yes | Max wall-clock time (`HH:MM:SS`) |
| `gpus` | int | no | GPUs per task |
| `gpu_type` | string | no | Preferred GPU type (e.g., `a100`) |

If `gpus` is present, the `gpu_array` template is used; otherwise `cpu_array`.

## auto_retry

Per-category retry policy. When set, `hpc-mapreduce failures` annotates each
failure cluster with a `retry_advice` block listing which task IDs are still
eligible for an automated retry (attempts so far < `max_attempts`) and which
have hit the cap.

```yaml
profiles:
  ml_ridge:
    auto_retry:
      gpu_oom:        { max_attempts: 1, mem_multiplier: 1.5 }
      system_oom:     { max_attempts: 1, mem_multiplier: 1.5 }
      walltime:       { max_attempts: 1, walltime_multiplier: 2.0 }
      node_failure:   { max_attempts: 2 }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `max_attempts` | int | yes | Per-task retry cap for this category. Tasks at this count are blocked from auto-retry. |
| `mem_multiplier` | float | no | Recommended memory multiplier for the next attempt (advisory; the framework echoes it back, the caller computes the concrete value). |
| `walltime_multiplier` | float | no | Recommended walltime multiplier (advisory). |
| `gpus_multiplier` | float | no | Recommended GPU count multiplier (advisory). |

Categories must match the failure-cluster categories emitted by
`hpc-mapreduce failures`: `gpu_oom`, `system_oom`, `walltime`,
`node_failure`, `import_error`, `file_not_found`, `permission_denied`,
`disk_full`, `python_traceback`, `unknown`.

The framework does not apply the multipliers — it surfaces the policy and
eligibility lists, and the caller (agent or human) decides whether to
issue `hpc-mapreduce resubmit` with the computed overrides.

## results

| Field | Type | Required | Description |
|---|---|---|---|
| `dir` | string | no | Result directory template. See *Result directory templating* below |
| `pattern` | string | no | Glob pattern for result files |
| `aggregate_cmd` | string | no | Fan-in command after all tasks complete |
| `summary_pattern` | string | no | Glob for summary files to download after aggregation |

### Result directory templating

`results.dir` is stored as `result_dir_template` in the per-run sidecar
and resolved per-task on the cluster by the dispatcher and combiner via
`template.format(task_id=, run_id=, **tasks.resolve(i))`. Supported
placeholders:

| Placeholder | Scope | Resolution |
|---|---|---|
| `{task_id}` | per-task | 0-based integer index. |
| `{run_id}` | per-task | The run's identifier (from the per-run sidecar). |
| `{<kwarg>}` | per-task | Any key returned by `tasks.resolve(i)` (e.g. `{model}`, `{seed}`). |

Run-level partitioning (e.g. `{date}`, `{git_sha}`) is achieved by
baking those values into the template at submit time, not via reserved
placeholders — for example, the agent can render
`f"results/{date}/{git_sha}/{{model}}_{{seed}}"` once and store the
fully-resolved-at-the-run-level form in the sidecar.

Examples:

```yaml
results:
  dir: "results/{model}_{seed}"           # one dir per task
  dir: "runs/2026-04-29/abc1234/{model}"  # run-level prefix already baked
```

## constraints

Declared cluster constraints for throughput optimization. Constraints can be defined in two places:

- **`clusters.yaml`** (cluster-level): applies to all jobs on that cluster
- **`hpc.yaml` profiles** (profile-level): per-experiment overrides

Profile-level constraints override cluster-level constraints **field-by-field** — any field set in the profile takes precedence, while unset fields fall back to the cluster default.

| Field | Type | Required | Description |
|---|---|---|---|
| `max_array_size` | int | no | Max tasks per job array (default: 1000) |
| `max_walltime` | string | no | Max wall time per job HH:MM:SS (default: "24:00:00") |
| `max_concurrent_jobs` | int | no | Max jobs running simultaneously (default: 10) |
| `est_spin_up` | string | no | Estimated spin-up overhead (e.g. "5m", default: "5m") |
| `est_task_duration` | string | no | Estimated duration per task (e.g. "10m", "1h30m"). Profile-level only. Used by the throughput optimizer to estimate total wall-clock time and plan wave scheduling. |

## Interface mismatches (`.hpc/tasks.py`)

When the framework's parallelization axis (Cartesian grid, chunking,
date windows, file lists, GPU device IDs, ...) doesn't match the
executor's native CLI, the **bridge is `.hpc/tasks.py`** — a small
user-written Python module exposing `total()` and `resolve(task_id)`.
`resolve(task_id)` returns the kwargs the dispatcher will export as env
vars before exec'ing the executor command from the per-run sidecar.

The agent generates `.hpc/tasks.py` once during the first `/submit`
(see `slash_commands/commands/submit.md` Step 6), commits it, and never
overwrites it. It lives in the experiment repo, is git-tracked, and is
fully inspectable by the user.

```python
# .hpc/tasks.py — eager-materialized, the only convention
import itertools
_TASKS = [
    {"seed": s, "model": m}
    for s, m in itertools.product([42, 1337], ["v1", "v2"])
]
def total(): return len(_TASKS)
def resolve(i): return _TASKS[i]
```

The canonical reference at `hpc_mapreduce/templates/tasks_example.py`
shows three patterns inline (Cartesian product, chunking by row count,
date-window backtest); the agent helps the user keep whichever applies
and delete the rest. The framework ships **no** library primitives like
`grid()` / `chunks()` / `windows()` — the diversity of axes lives in
user code, not framework config.

## cluster_envs

Optional per-cluster environment overrides. Keyed by cluster name, then by env_group name. Profiles reference these via `env_group`.

```yaml
cluster_envs:
  hoffman2:
    ml: { modules: "python gcc" }
    dl: { modules: "conda cuda/12.3", conda_env: <your_env> }
  discovery:
    ml: { modules: "python" }
    dl: { modules: "", conda_env: <your_env> }
```

## How It Works

1. `/submit` reads `hpc.yaml` and offers existing profiles as shortcuts.
2. The agent walks the user through writing `.hpc/tasks.py` once
   (`total()` + `resolve(task_id)` returning the per-task kwargs);
   subsequent submits reuse it byte-for-byte.
3. A per-run sidecar `.hpc/runs/<run_id>.json` records the executor
   command, `result_dir_template`, `cmd_sha`, and `wave_map` for this run.
4. `deploy_runtime` scp's `.hpc/_hpc_dispatch.py`, `.hpc/_hpc_combiner.py`,
   and `.hpc/templates/*` to the cluster.
5. The job template runs `python3 .hpc/_hpc_dispatch.py` as its executor.
6. The dispatcher imports `.hpc/tasks.py`, reads the run sidecar for the
   executor command and result_dir template, formats result_dir from
   kwargs, exports each kwarg as an env var (uppercased + `HPC_KW_*`),
   and execs the executor command.

The experiment author's code receives all kwargs as ordinary env vars —
no awareness of HPC or task IDs required.

---

## Examples

### Single-Profile, Single-Stage (simplest)

```yaml
project: my_experiment
cluster: hoffman2
remote_path: /u/home/<your_user>/my_experiment

profiles:
  sweep:
    run: "python3 -m my_experiment.train"
    env: { modules: "python gcc" }
    resources: { cpus: 1, mem: "16G", walltime: "4:00:00" }
    results:
      dir: "results/{model}_{lr}_{seed}"
      pattern: "*.csv"
    rsync_exclude: [.git/, results/, __pycache__]
```

The 18-task Cartesian product (`{model: [ridge, xgboost, lightgbm], lr: [0.01, 0.001], seed: [1, 2, 3]}`) lives in `.hpc/tasks.py`:

```python
import itertools
_TASKS = [
    {"model": m, "lr": lr, "seed": s}
    for m, lr, s in itertools.product(
        ["ridge", "xgboost", "lightgbm"], [0.01, 0.001], [1, 2, 3]
    )
]
def total(): return len(_TASKS)
def resolve(i): return _TASKS[i]
```

The dispatcher exports `MODEL`, `LR`, `SEED` (and `HPC_KW_MODEL`,
`HPC_KW_LR`, `HPC_KW_SEED`) as env vars before running the executor.

### Multi-Stage Profile (train → test pipeline)

```yaml
project: myexp_b
cluster: discovery
remote_path: /home1/<your_user>/myexp_b

cluster_envs:
  discovery:
    dl: { conda_env: <your_env> }

profiles:
  cfm:
    stages:
      train:
        run: "python scripts/train.py"
        env_group: dl
        resources: { cpus: 8, mem: "64G", walltime: "2:00:00", gpus: 1, gpu_type: a100 }
        results:
          pattern: "checkpoints/best.pt"

      generate:
        depends_on: train
        run: "python scripts/generate.py --checkpoint checkpoints/best.pt"
        # Date-window axis lives in .hpc/tasks.py — e.g.
        #   _TASKS = [
        #       {"window_start": w.isoformat(), "window_end": (w + timedelta(weeks=26)).isoformat()}
        #       for w in date_range(date(2020,1,1), date(2024,12,31), step=timedelta(weeks=26))
        #   ]
        env_group: dl
        resources: { cpus: 8, mem: "64G", walltime: "0:30:00", gpus: 1, gpu_type: a100 }
        results:
          pattern: "samples/seed_*/chunk_*.pt"

      evaluate:
        depends_on: generate
        run: "python scripts/evaluate.py --checkpoint checkpoints/best.pt"
        env_group: dl
        resources: { cpus: 8, mem: "64G", walltime: "0:30:00", gpus: 1, gpu_type: a100 }
        results:
          pattern: "eval_results/metrics.json"
          summary_pattern: "eval_results/*"

rsync_exclude: [.git/, samples/, __pycache__, "*.pyc", .mypy_cache/, data/]
```

### Single-Profile Shorthand (no profiles key)

For the simplest case, `run`/`grid`/`resources` can live at the top level without a `profiles` wrapper:

```yaml
project: myexp_a
cluster: hoffman2
remote_path: /u/home/<your_user>/myexp_a

run: "python3 train.py"
resources: { cpus: 1, mem: "8G", walltime: "1:00:00" }
```

This is equivalent to a single profile named after the project.
Parallelization (e.g. sweeping `lr` and `batch_size`) lives in
`.hpc/tasks.py`, scaffolded by `/submit` Step 6.
