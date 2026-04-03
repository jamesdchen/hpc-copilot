# claude-hpc

A Claude Code plugin for running experiment repos on HPC clusters (SGE/SLURM). Point Claude at your executors, describe what you want to run, and hpc_mapreduce handles the rest — grid expansion, dispatch, monitoring, and result aggregation — all via SSH.

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Organize your experiment repo

Keep standalone executor scripts in a dedicated directory, separate from shared utilities:

```
my_experiment/
├── executors/           # or src/ — each file is a runnable experiment
│   ├── ml_ridge.py      # python3 executors/ml_ridge.py --help
│   ├── ml_xgboost.py
│   └── dl_patchts.py
├── lib/                 # shared utilities (not executors)
│   ├── loading.py
│   └── transforms.py
└── data/
```

Each executor accepts experiment-specific arguments (`--horizon`, `--start`, `--end`, `--features`, etc.). No HPC awareness is needed — all parameters arrive as CLI flags.

### 3. Run

```
/submit    → discovers executors, builds grid conversationally, syncs code, submits
/monitor   → tracks completion per grid point, diagnoses failures, auto-resubmits
/aggregate → validates completeness, runs aggregation, downloads summaries
```

**Example conversation:**

```
You: /submit run ridge and xgboost with horizon=[1, 5, 25]

Claude: I found these executors in src/:
  ml_ridge.py    — --horizon, --start, --end, --output-file
  ml_xgboost.py  — --horizon, --start, --end, --output-file

Proposed plan:
  Cluster: hoffman2 (SGE)
  Grid: executor=[ml_ridge, ml_xgboost] × horizon=[1, 5, 25] → 6 grid points
  Backtest: 2020-01-01 to 2024-12-31 (6M periods) → 10 periods
  Total: 6 × 10 = 60 tasks
  Resources: 1 CPU, 16G, 4:00:00
  Confirm?

You: yes

Claude: Submitted job 12345678 (60 tasks). Run /monitor to track progress.
```

No config files required. Claude discovers your executors, detects environment needs from imports, and suggests resources based on executor type.

## How It Works

1. Claude reads your executor scripts and their `--help` output
2. You describe what to run in natural language — Claude builds the grid
3. A `_hpc_dispatch.json` manifest maps each task ID to its full command
4. A standalone `_hpc_dispatch.py` script (zero dependencies) is deployed to the cluster
5. The job template runs the dispatch script, which looks up the command for its task ID
6. Your code runs with the right params — no HPC awareness needed

### Parallelism Model

Grid parameters (including optional backtest time periods) are expanded via Cartesian product. Each combination becomes one HPC task. Executors receive all parameters as CLI arguments — no HPC awareness needed.

With backtest enabled, total tasks = grid points × time periods.

## Commands

| Command | What it does |
|---------|-------------|
| `/submit` | Discover executors, build grid conversationally, sync code, submit array jobs |
| `/monitor` | Poll status, diagnose failures, auto-resubmit, self-schedule next check |
| `/aggregate` | Validate completeness, run aggregation on cluster, download summaries |
## Configuration

### `clusters.yaml` (required)

Cluster infrastructure definitions. Ships with claude-hpc in `config/clusters.yaml`:

```yaml
hoffman2:
  host: hoffman2.idre.ucla.edu
  user: jamesdc1
  scheduler: sge
  scratch: /u/scratch/j/jamesdc1
  modules: [python/3.11.9]
  conda_source: /u/local/apps/anaconda3/2024.06/etc/profile.d/conda.sh
  conda_envs: [harxhar-dl]          # optional — Claude presents these as options
  gpu_types: [a100, h200, a6000]
```

### `hpc.yaml` (optional)

If you prefer declarative config over conversational setup, add `hpc.yaml` to your experiment repo. Claude will read it as pre-populated preferences. See [`docs/schema.md`](docs/schema.md) for the full spec.

### Caching

Claude remembers your preferences (cluster, executor directory, environment, resources) across conversations via Claude Code memory. The `_hpc_dispatch.json` manifest serves as the submission record for monitoring and resubmission.

## Job Templates

| Template | SGE | SLURM |
|----------|-----|-------|
| CPU array | `templates/sge/cpu_array.sh` | `templates/slurm/cpu_array.slurm` |
| GPU array | `templates/sge/gpu_array.sh` | `templates/slurm/gpu_array.slurm` |

Templates are parameterized via environment variables injected at submission time. Auto-selected based on detected GPU requirements.

## Supported Clusters

| Cluster | Institution | Scheduler |
|---------|------------|-----------|
| Hoffman2 | UCLA IDRE | SGE |
| Discovery | USC CARC | SLURM |

Cluster connection details are in `config/clusters.yaml`.

## Python API

```python
from hpc_mapreduce import expand_grid, build_task_manifest, total_tasks
from hpc_mapreduce import expand_backtest, ClusterConstraints, parse_constraints
from hpc_mapreduce import load_clusters_config, get_template_path, _PACKAGE_ROOT
from hpc_mapreduce.infra.backends import get_backend
```
