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
| `run` | string | yes | Shell command. Grid params appended as `--key value` CLI args |
| `grid` | map | no | Parameter grid — Cartesian product generates tasks |
| `resources` | map | yes | Resource request per task |
| `env` | map | no | Environment setup (modules, conda_env) |
| `env_group` | string | no | Key into `cluster_envs[cluster]` for env overrides |
| `results` | map | no | Result collection config |
| `backtest` | map | no | Time-based parallelism (date range split into periods) |
| `constraints` | map | no | Cluster constraints for throughput optimization |
| `gpu_fallback` | list[str] | no | Ordered GPU types to try |
| `max_retries` | int | no | Max auto-resubmissions on failure |

### Multi-Stage Profiles

When a profile contains a `stages` key, each stage has the same fields as a single-stage profile, plus:

| Field | Type | Required | Description |
|---|---|---|---|
| `depends_on` | string or list[str] | no | Stage(s) that must complete first |

Stages without `grid` run as single jobs. Stages with `grid` get fan-out (parallel tasks) → fan-in (`results.aggregate_cmd`).

## grid

Map of parameter_name → list of values. Cartesian product = one task per combo (or multiplied by backtest periods if configured).

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

## results

| Field | Type | Required | Description |
|---|---|---|---|
| `dir` | string | no | Result directory template. Supports `{run_id}` placeholder |
| `pattern` | string | no | Glob pattern for result files |
| `aggregate_cmd` | string | no | Fan-in command after all tasks complete |
| `summary_pattern` | string | no | Glob for summary files to download after aggregation |

## backtest

Optional time-based parallelism. Splits a date range into periods
that become additional grid parameters (paired, not cross-producted).

| Field | Type | Required | Description |
|---|---|---|---|
| `start` | string | yes | Start date (YYYY-MM-DD) |
| `end` | string | yes | End date (YYYY-MM-DD) |
| `chunk_duration` | string | yes | Duration per period (e.g. "6M", "1Y", "30D") |
| `start_arg` | string | no | CLI flag for period start (default: `"--start"`) |
| `end_arg` | string | no | CLI flag for period end (default: `"--end"`) |

Total HPC tasks = grid_points × time_periods.

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

## cluster_envs

Optional per-cluster environment overrides. Keyed by cluster name, then by env_group name. Profiles reference these via `env_group`.

```yaml
cluster_envs:
  hoffman2:
    ml: { modules: "python gcc" }
    dl: { modules: "conda cuda/12.3", conda_env: harxhar-dl }
  discovery:
    ml: { modules: "python" }
    dl: { modules: "", conda_env: project-cucuringu }
```

## How It Works

1. `/submit` reads `hpc.yaml` and offers existing profiles as shortcuts
2. The selected profile's grid is expanded into individual tasks
3. A `_hpc_dispatch.json` manifest maps each task ID to its command + result dir
4. A standalone `_hpc_dispatch.py` script is deployed alongside the manifest
5. The job template runs `python3 _hpc_dispatch.py` as its executor
6. The dispatch script reads the manifest and executes the command for its task ID

The experiment author's code receives all params as normal CLI args — no awareness of HPC or task IDs required.

---

## Examples

### Single-Profile, Single-Stage (simplest)

```yaml
project: my_experiment
cluster: hoffman2
remote_path: /u/home/j/jamesdc1/my_experiment

profiles:
  sweep:
    run: "python3 -m my_experiment.train"
    grid:
      model: [ridge, xgboost, lightgbm]
      lr: [0.01, 0.001]
      seed: [1, 2, 3]
    env: { modules: "python gcc" }
    resources: { cpus: 1, mem: "16G", walltime: "4:00:00" }
    results:
      dir: "results/{run_id}"
      pattern: "*.csv"
    rsync_exclude: [.git/, results/, __pycache__]
```

### Multi-Stage Profile (train → test pipeline)

```yaml
project: vol_cfm
cluster: discovery
remote_path: /home1/jc_905/vol_cfm

cluster_envs:
  discovery:
    dl: { conda_env: project-cucuringu }

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
        backtest: { start: "2020-01-01", end: "2024-12-31", chunk_duration: "6M" }
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
project: quick_sweep
cluster: hoffman2
remote_path: /u/home/j/jamesdc1/quick_sweep

run: "python3 train.py"
grid:
  lr: [0.01, 0.001]
  batch_size: [32, 64]
resources: { cpus: 1, mem: "8G", walltime: "1:00:00" }
```

This is equivalent to a single profile named after the project.
