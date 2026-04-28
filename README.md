# claude-hpc

HPC orchestrator for parameter-grid experiments on SGE/SLURM clusters. Two surfaces over one core:

- **Slash commands for humans** in Claude Code (`/submit`, `/status`, `/aggregate`, `/build-executor`, `/preflight`) — interactive, prompts you for cluster + grid params.
- **CLI for agents and automation** (`hpc-mapreduce <subcommand>`) — JSON-in, JSON-out, exit codes. Designed to be invoked via the Bash tool by orchestrators like [MARs](https://github.com/FredFang1216/MARs).

Both go through the same atomic-ops layer (`slash_commands/runner.py`), so cross-surface state (in-flight runs, journal records) is shared automatically.

## Quick Start

### For humans (Claude Code)

```bash
pip install -e .
```
Open the repo in Claude Code, then:
- `/preflight` — verify SSH agent + cluster reachability.
- `/submit` — answer prompts about cluster, executor, grid params.
- `/status` to monitor, `/aggregate` to collect results.

### For agents and automation

```bash
pip install claude-hpc
hpc-mapreduce preflight --cluster hoffman2     # health check
hpc-mapreduce submit --spec spec.json          # JSON envelope on stdout
hpc-mapreduce status --run-id <id>             # one-shot snapshot; poll as needed
hpc-mapreduce aggregate --run-id <id> --wave 1 # combiner + result pull
```
Stdout is a single-line JSON envelope: `{"ok": true, "idempotent": ..., "data": {...}}` or `{"ok": false, "error_code": ..., "retry_safe": ..., "remediation": ...}`. Exit codes: 0 ok, 1 user error, 2 cluster/network, 3 internal. Full schema in [`docs/cli-spec.md`](docs/cli-spec.md); JSON Schema files for runtime validation under `hpc_mapreduce/schemas/`.

### Using with MARs

claude-hpc plugs into MARs as a `Bash`-invokable tool from the existing
`experiment-runner` agent. See **[`docs/mars-integration.md`](docs/mars-integration.md)**
for the proposal package: Bun.spawn env block, `error_code` → retry
policy table, troubleshooting, and the paste-ready
[`docs/mars/experiment-runner.snippet.md`](docs/mars/experiment-runner.snippet.md)
for `agents/experiment-runner.md`.

The most common first-time failure is `Bun.spawn`'s empty default env
dropping `SSH_AUTH_SOCK`. `hpc-mapreduce status`/`aggregate`/`reconcile`
now fail fast with `error_code: "ssh_unreachable"` (exit 2) instead of
hanging on auth — run `hpc-mapreduce preflight` first to verify the spawn
env. claude-hpc does not kill cluster jobs by design (`settings.json`
denies `scancel`/`qdel`); if MARs decides a run is bad, stop polling and
let it expire.

---

## Standalone usage

### Organize your experiment repo

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

### Run

```
/preflight → verify SSH agent + cluster reachability before first submit
/submit    → discovers executors, builds grid conversationally, syncs code, submits
/status    → tracks completion per grid point, diagnoses failures, auto-resubmits
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
  Total: 6 tasks
  Resources: 1 CPU, 16G, 4:00:00
  Confirm?

You: yes

Claude: Submitted job 12345678 (6 tasks). Run /status to track progress.
```

No config files required. Claude discovers your executors by reading their source and `--help`, then suggests resources conversationally based on the executor and your input.

## How It Works

The boundary between claude-hpc and your experiment repo is documented in [`docs/boundary-contract.md`](docs/boundary-contract.md) and enforced by `tests/test_boundary_contract.py`.

1. Claude reads your executor scripts and their `--help` output
2. You describe what to run in natural language — Claude builds the grid
3. A `_hpc_dispatch.json` manifest maps each task ID to its full command
4. A standalone `_hpc_dispatch.py` script (zero dependencies) is deployed to the cluster
5. The job template runs the dispatch script, which looks up the command for its task ID
6. Your code runs with the right params — no HPC awareness needed

### Parallelism Model

Grid parameters are expanded via Cartesian product. Each combination becomes one HPC task. Executors receive all parameters as CLI arguments — no HPC awareness needed.

### Throughput Optimization

claude-hpc automatically optimizes job submissions for cluster constraints. When constraints are configured (max array size, walltime, concurrent job limits), the optimizer packs tasks into batched waves:

- Tasks are split into arrays of ≤max_array_size
- Arrays are grouped into waves of ≤max_concurrent_jobs
- Waves are staggered via scheduler dependencies (SLURM `--dependency`, SGE `-hold_jid`)
- Total wall-clock time is estimated when per-task duration is known

Configure constraints in `clusters.yaml` (cluster-level) or `hpc.yaml` profiles (per-experiment overrides).

## Commands

| Command | What it does |
|---------|-------------|
| `/preflight` | Verify SSH agent, ssh/rsync on PATH, clusters.yaml parses, cluster reachable |
| `/submit` | Discover executors, build grid conversationally, sync code, submit array jobs |
| `/status` | Poll status, diagnose failures, auto-resubmit, self-schedule next check |
| `/aggregate` | Validate completeness, run aggregation on cluster, download summaries |
| `/build-executor` | Scaffold a new executor or shim from a starter template |
## Configuration

### `clusters.yaml` (required)

Cluster infrastructure definitions. Ships inside the package at `hpc_mapreduce/config/clusters.yaml`. Override the active path with `HPC_CLUSTERS_CONFIG=/your/clusters.yaml` (useful for MARs users who want to keep their cluster definitions outside the package):

```yaml
hoffman2:
  host: hoffman2.idre.ucla.edu
  user: <your_user>
  scheduler: sge
  scratch: <your_scratch>
  modules: [python/3.11.9]
  conda_source: /u/local/apps/anaconda3/2024.06/etc/profile.d/conda.sh
  conda_envs: [<your_env>]          # optional — Claude presents these as options
  gpu_types: [a100, h200, a6000]
```

### `hpc.yaml` (optional)

If you prefer declarative config over conversational setup, add `hpc.yaml` to your experiment repo. Claude will read it as pre-populated preferences. See [`docs/schema.md`](docs/schema.md) for the full spec.

### Caching

Claude remembers your preferences (cluster, executor directory, environment, resources) across conversations via Claude Code memory. The `_hpc_dispatch.json` manifest serves as the submission record for monitoring and resubmission.

## Job Templates

| Template | SGE | SLURM |
|----------|-----|-------|
| CPU array | `hpc_mapreduce/templates/sge/cpu_array.sh` | `hpc_mapreduce/templates/slurm/cpu_array.slurm` |
| GPU array | `hpc_mapreduce/templates/sge/gpu_array.sh` | `hpc_mapreduce/templates/slurm/gpu_array.slurm` |

Templates are parameterized via environment variables injected at submission time. Resolve paths via `hpc_mapreduce.get_template_path(scheduler, template)`. The GPU template is used when the configured resources include `gpus`; otherwise the CPU template is used.

## Supported Clusters

| Cluster | Institution | Scheduler |
|---------|------------|-----------|
| Hoffman2 | UCLA IDRE | SGE |
| Discovery | USC CARC | SLURM |

Cluster connection details are in `hpc_mapreduce/config/clusters.yaml` (or whatever `HPC_CLUSTERS_CONFIG` points at).

## Python API

```python
from hpc_mapreduce import expand_grid, build_task_manifest, total_tasks
from hpc_mapreduce import ClusterConstraints, parse_constraints
from hpc_mapreduce import load_clusters_config, get_template_path, _PACKAGE_ROOT
from hpc_mapreduce.infra.backends import get_backend
```
