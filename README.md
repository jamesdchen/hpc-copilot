# claude-hpc

A standalone Claude Code plugin for running any experiment repo on HPC clusters (SGE/SLURM). Point it at your code, define a parameter grid, and claude-hpc handles the rest — chunking, submission, monitoring, failure recovery, and result collection — all via SSH.

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Add `hpc.yaml` to your experiment repo

```yaml
project: my_experiment
cluster: hoffman2
remote_path: /u/home/j/jamesdc1/my_experiment

run: "python3 train.py"

grid:
  model: [ridge, xgboost, lightgbm]
  lr: [0.01, 0.001]
  seed: [1, 2, 3]

env:
  modules: "python gcc"

resources:
  cpus: 1
  mem: "16G"
  walltime: "4:00:00"

results:
  dir: "results/{run_id}"
  pattern: "*.csv"
```

### 3. Run

```
/submit    → expands grid (18 tasks), syncs code, submits to cluster
/monitor   → tracks per-grid-point completion, auto-resubmits failures
/aggregate → runs aggregation, downloads summaries
```

That's it. Your experiment code receives grid params as CLI args (`--model ridge --lr 0.01 --seed 1`). No HPC awareness needed.

## How It Works

1. claude-hpc reads `hpc.yaml` and computes the Cartesian product of the `grid`
2. A `_hpc_dispatch.json` manifest maps each task ID to its full command
3. A standalone `_hpc_dispatch.py` script (zero dependencies) is deployed to the cluster
4. The job template runs the dispatch script, which looks up the command for its task ID
5. Your code runs with the right params — no `CHUNK_ID`, no env var parsing

### Two Layers of Parallelism

| Layer | What | Who handles it |
|-------|------|----------------|
| **Grid** | Different param combos (model × lr × seed) | claude-hpc (automatic) |
| **Data chunking** | Splitting one run's data across N workers | Opt-in via `chunking:` section |

```yaml
# Optional: split each grid point into 100 data chunks
chunking:
  total: 100
  chunk_arg: "--chunk-id"
  total_arg: "--total-chunks"
```

With chunking, total tasks = grid points × chunks (e.g., 18 × 100 = 1,800).

## Commands

| Command | What it does |
|---------|-------------|
| `/submit` | Expand grid, sync code, submit array jobs |
| `/monitor` | Poll status, diagnose failures, auto-resubmit, self-schedule next check |
| `/aggregate` | Validate completeness, run aggregation on cluster, download summaries |

## Configuration

### `hpc.yaml` (Experiment Manifest) — recommended

The simple path. Define your run command and parameter grid. See [`config/schema.md`](config/schema.md) for the full spec.

### `project.yaml` (Stage-Based) — advanced

For projects that need fine-grained control over executor commands, multi-stage pipelines with dependencies, or custom chunking logic. See [`config/schema.md`](config/schema.md).

Both formats are supported. If `hpc.yaml` exists, it takes precedence for `/submit`.

## Job Templates

| Template | SGE | SLURM |
|----------|-----|-------|
| CPU array | `templates/sge/cpu_array.sh` | `templates/slurm/cpu_array.slurm` |
| GPU array | `templates/sge/gpu_array.sh` | `templates/slurm/gpu_array.slurm` |

Templates are parameterized via environment variables injected at submission time. Auto-selected based on `resources.gpus` in your config.

## Supported Clusters

| Cluster | Institution | Scheduler |
|---------|------------|-----------|
| Hoffman2 | UCLA IDRE | SGE |
| Discovery | USC | SLURM |

Cluster connection details are in `config/clusters.yaml`.

## Chunking Protocol

claude-hpc provides a chunking protocol so experiment executors don't need to
implement their own parallelisation logic.

```python
from hpc.chunking import chunk_context

ctx = chunk_context()                          # no-op locally (chunk 0 of 1)
my_range = ctx.split(range(train_win, N))      # full range locally, subset on HPC
results.to_csv(ctx.output_path())              # ./results_chunk_1.csv locally
```

- `chunk_context()` reads `CHUNK_ID`, `TOTAL_CHUNKS`, `RESULT_DIR` from env vars (set by claude-hpc job templates)
- Defaults to chunk 0 of 1 for local development — executor processes everything
- `ctx.split()` accepts a `range` or `int` and returns the sub-range for this chunk
- `ctx.output_path()` generates the standard `results_chunk_{id+1}.csv` filename
- `collect_chunks(result_dir)` stitches chunk CSVs back into a single sorted DataFrame (fan-in companion to `chunk_context`)

## Python API

```python
from hpc import expand_grid, build_task_manifest, load_manifest, build_manifest_env
from hpc import load_clusters_config, load_project_config, get_template_path
from hpc.backends import get_backend
```
