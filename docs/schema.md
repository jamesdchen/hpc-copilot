# hpc.yaml Specification

## Top-Level Fields

These are shared across all profiles:

| Field | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | Short project name (used in job names, paths, logs) |
| `cluster` | string | yes | Cluster key matching an entry in `clusters.yaml` |
| `remote_path` | string | yes | Absolute path on the remote cluster |
| `rsync_exclude` | list[str] | no | Patterns passed to `rsync --exclude` during sync |
| `experiment_paths` | list[str] | no | Glob patterns for experiment YAML configs |
| `registries` | map | no | Importable registries in `"module.path:ATTR"` format |
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
| `grid` | map | yes | Parameter grid — Cartesian product generates tasks |
| `resources` | map | yes | Resource request per task |
| `env` | map | no | Environment setup (modules, conda_env) |
| `env_group` | string | no | Key into `cluster_envs[cluster]` for env overrides |
| `results` | map | no | Result collection config |
| `chunking` | map | no | Data chunking within each grid point |
| `gpu_fallback` | list[str] | no | Ordered GPU types to try |
| `max_retries` | int | no | Max auto-resubmissions on failure |

### Multi-Stage Profiles

When a profile contains a `stages` key, each stage has the same fields as a single-stage profile, plus:

| Field | Type | Required | Description |
|---|---|---|---|
| `depends_on` | string or list[str] | no | Stage(s) that must complete first |

Stages without `grid` run as single jobs. Stages with `grid` get fan-out (parallel tasks) → fan-in (`results.aggregate_cmd`).

## grid

Map of parameter_name → list of values. Cartesian product = one task per combo (or N tasks if chunking).

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

## chunking

Splits each grid point into N data chunks for additional parallelism.

| Field | Type | Required | Description |
|---|---|---|---|
| `total` | int | yes | Chunks per grid point |
| `chunk_arg` | string | no | CLI flag for chunk index (default: `"--chunk-id"`) |
| `total_arg` | string | no | CLI flag for total chunks (default: `"--total-chunks"`) |

Total HPC tasks = grid_points × `total`.

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

1. claude-hpc reads `hpc.yaml` and selects a profile (and stage, if multi-stage).
2. The grid is expanded into individual tasks.
3. A `_hpc_dispatch.json` manifest maps each task ID to its command + result dir.
4. A standalone `_hpc_dispatch.py` script is deployed alongside the manifest.
5. The job template runs `python3 _hpc_dispatch.py` as its executor.
6. The dispatch script reads the manifest and executes the command for its task ID.

The experiment author's code receives grid params as normal CLI args — no awareness of HPC, chunking, or task IDs required (unless using `chunking`).

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

### Multi-Profile (harxhar pattern — ML + DL in one repo)

```yaml
# ML/DL backtesting pipelines for financial volatility forecasting.
project: harxhar
cluster: hoffman2
remote_path: /u/home/j/jamesdc1/project-cucuringu/harxhar

experiment_paths: ["projects/ml/experiments/*.yaml"]
registries:
  models: "projects.ml.models.registry:ALL_MODELS"
  features: "projects.ml.features.feature_groups:FEATURE_TYPES"
  subgroups: "projects.ml.features.feature_groups:SUBGROUPS"

cluster_envs:
  hoffman2:
    ml: { modules: "python gcc" }
    dl: { modules: "conda cuda/12.3", conda_env: harxhar-dl }

profiles:
  ml:
    run: "python3 -m projects.ml.cli.executor"
    grid:
      model: [ridge, xgboost, lightgbm, random_forest]
      features: [har, pca, ae]
    chunking: { total: 100, chunk_arg: "--chunk-id", total_arg: "--total-chunks" }
    env_group: ml
    resources: { cpus: 1, mem: "16G", walltime: "4:00:00" }
    results:
      dir: "results/{run_id}"
      pattern: "results_chunk_*.csv"
      aggregate_cmd: "python projects/ml/scripts/aggregate.py"
      summary_pattern: "*_summary*.csv"

  dl:
    run: "python3 -m projects.dl.cli.gpu_executor"
    grid:
      experiment: [patchts, ae_ridge]
    chunking: { total: 10, chunk_arg: "--chunk-id", total_arg: "--total-chunks" }
    env_group: dl
    resources: { cpus: 4, mem: "16G", walltime: "6:00:00", gpus: 2, gpu_type: a100 }
    gpu_fallback: [a100, h200, a6000, h100, v100, rtx2080ti]
    max_retries: 3
    results:
      dir: "results/{run_id}"
      pattern: "results_chunk_*.csv"
      aggregate_cmd: "python -m projects.dl.scripts.aggregate"
      summary_pattern: "*_summary*.csv"

rsync_exclude: [.git/, results/, results_scaling_laws/, __pycache__/, "*.pyc", .mypy_cache/, all30min/, .claude/]
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
        chunking: { total: 10 }
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
