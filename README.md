# claude-hpc

HPC orchestrator for array-batch experiments on SGE/SLURM clusters. Two surfaces over one core:

- **Slash commands for humans** in Claude Code (`/submit-hpc`, `/monitor-hpc`, `/aggregate-hpc`, `/campaign-hpc`, `/preflight`) — interactive markdown templates in `slash_commands/commands/*.md` that walk you through choosing a cluster and authoring `.hpc/tasks.py`. Executor scaffolding is folded into `/submit-hpc` Step 1; preflight is folded into `/submit-hpc` Step 6b as an idempotent gate (with `/preflight` still available as a standalone diagnostic).
- **CLI for agents and automation** (`hpc-mapreduce <subcommand>`) — JSON-in, JSON-out, exit codes. Designed to be invoked via the Bash tool by orchestrators like [MARs](https://github.com/FredFang1216/MARs). This is a POSIX-native agent surface: any tool that can shell out and parse JSON can drive a cluster — see [`docs/reference/agent-surface.md`](docs/reference/agent-surface.md).

Both surfaces invoke `hpc-mapreduce <subcommand>`. The slash commands are pure markdown that orchestrate the binary; the binary's atomic-ops layer (`claude_hpc.runner`) ensures cross-surface state — in-flight runs, journal records under `~/.claude/hpc/<repo_hash>/` — is shared automatically.

## Quick Start

### For humans (Claude Code)

```bash
pip install -e .
```
Open the repo in Claude Code, then:
- `/preflight` (optional) — verify SSH agent + cluster reachability. `/submit-hpc` auto-runs this as a cached gate, so you only need it for ad-hoc diagnostics.
- `/submit-hpc` — answer prompts about cluster, executor, grid params. Scaffolds the executor inline if none exists.
- `/monitor-hpc` to monitor, `/aggregate-hpc` to collect results.

### For agents and automation

```bash
pip install claude-hpc
hpc-mapreduce preflight --cluster hoffman2                    # health check
hpc-mapreduce interview --spec intent.json --campaign-dir <d> # persist campaign intent next to tasks.py
hpc-mapreduce recall --root ~/experiments --task-kind <kind>  # query past interviews for next-interview grounding
hpc-mapreduce submit --spec spec.json                          # JSON envelope on stdout
hpc-mapreduce status --run-id <id>                             # one-shot snapshot; poll as needed
hpc-mapreduce aggregate --run-id <id> --wave 1                 # combiner + result pull
hpc-mapreduce inspect-cluster --cluster <c>                    # per-node alloc/load/co-tenant snapshot
hpc-mapreduce runtime-prior --profile <p> --cluster <c>        # quantile rollup of past task runtimes
hpc-mapreduce plan-submit --profile <p> --cluster <c>          # constraint scorecard for /submit-hpc
```
Stdout is a single-line JSON envelope: `{"ok": true, "idempotent": ..., "data": {...}}` or `{"ok": false, "error_code": ..., "retry_safe": ..., "remediation": ...}`. Exit codes: 0 ok, 1 user error, 2 cluster/network, 3 internal. Full schema in [`docs/reference/cli-spec.md`](docs/reference/cli-spec.md); JSON Schema files for runtime validation under `hpc_mapreduce/schemas/`.

### Using with MARs

claude-hpc plugs into MARs as a `Bash`-invokable tool from the existing
`experiment-runner` agent. See **[`docs/workflows/mars-integration.md`](docs/workflows/mars-integration.md)**
for the proposal package: Bun.spawn env block, `error_code` → retry
policy table, troubleshooting, and the paste-ready
[`docs/workflows/mars/experiment-runner.snippet.md`](docs/workflows/mars/experiment-runner.snippet.md)
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
/submit    → discovers executors, walks you through .hpc/tasks.py, syncs code, submits
/monitor-hpc    → tracks completion per grid point, diagnoses failures, auto-resubmits
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

Claude: Submitted job 12345678 (6 tasks). Run /monitor-hpc to track progress.
```

No config files required. Claude discovers your executors by reading their source and `--help`, then suggests resources conversationally based on the executor and your input.

## How It Works

The boundary between claude-hpc and your experiment repo is documented in [`docs/reference/boundary-contract.md`](docs/reference/boundary-contract.md) and enforced by `tests/test_boundary_contract.py`.

1. Claude reads your executor scripts and their `--help` output.
2. You describe what to run in natural language — Claude walks you through writing `.hpc/tasks.py` once: a small Python module exposing `total()` and `resolve(task_id)` that returns the per-task kwargs. The file is committed to git and reused on every subsequent submit.
3. A per-run sidecar `.hpc/runs/<run_id>.json` records the executor command, result-dir template, `cmd_sha`, and wave map for this particular submission.
4. The framework executor `_hpc_dispatch.py` (zero deps, stdlib-only) is deployed to the cluster's `.hpc/` by `deploy_runtime`.
5. The job template runs the dispatcher, which imports your `.hpc/tasks.py`, calls `resolve(task_id)`, formats the result_dir, and execs your executor command with kwargs as env vars.
6. Your executor reads kwargs as ordinary env vars (uppercased + `HPC_KW_*`) — no HPC awareness needed.

### Parallelism Model

The parallelization axis lives entirely in user code (`.hpc/tasks.py`). The framework is agnostic to whether you're doing a Cartesian grid, chunking by row count, date-window backtests, or something else — it just calls `total()` and `resolve(i)`. The canonical reference at `hpc_mapreduce/templates/tasks_example.py` shows three patterns inline; the agent helps you keep whichever applies and delete the rest.

### Memory across campaigns

Two primitives — `interview` and `recall` — close the loop between consecutive campaigns. The interview agent (Claude Code or MARs) persists structured intent (`goal`, `task_count`, `budget`, `abort_if`, `task_generator`, `cluster_target`, `transcript`, provenance) into `<campaign_dir>/interview.json` next to the materialized `tasks.py`. The next interview calls `recall --root <experiments-dir>` to query past intents, returning recency-sorted summaries plus a 3-tier rollup (counts/histograms/quantiles, optional walltime aggregation, optional per-generator parameter envelopes). Observed ranges only — reasoning over them stays in the calling agent.

See [`docs/workflows/memory-across-campaigns.md`](docs/workflows/memory-across-campaigns.md) for the full flow, including the `task_generator` typed materializer (5 shapes: `enumerated`, `cartesian_product`, `items_x_seeds`, `numeric_logspace`, `numeric_linspace`) and the `~/.claude-hpc/config.json:experiment_roots` default-root config.

### Throughput Optimization

claude-hpc automatically optimizes job submissions for cluster constraints. When constraints are configured (max array size, walltime, concurrent job limits), the optimizer packs tasks into batched waves:

- Tasks are split into arrays of ≤max_array_size
- Arrays are grouped into waves of ≤max_concurrent_jobs
- Waves are staggered via scheduler dependencies (SLURM `--dependency`, SGE `-hold_jid`)
- Total wall-clock time is estimated when per-task duration is known

Configure constraints in `clusters.yaml` (cluster-level); per-experiment overrides resolved at `/submit` time are persisted to the run sidecar at `.hpc/runs/<run_id>.json`.

## Commands

| Command | What it does |
|---------|-------------|
| `/preflight` | Standalone: verify SSH agent, ssh/rsync on PATH, clusters.yaml parses, cluster reachable. `/submit-hpc` auto-runs the same checks as a 24h-cached gate, so direct invocation is mostly for ad-hoc diagnostics. |
| `/submit-hpc` | Discover executors (scaffolds inline if none found), build grid conversationally, write `.hpc/tasks.py` with FLAGS dict + `.hpc/cli.py` dispatcher, sync code, submit array jobs |
| `/monitor-hpc` | Poll status, diagnose failures, auto-resubmit, self-schedule next check |
| `/aggregate-hpc` | Validate completeness, run aggregation on cluster, download summaries |
| `/campaign-hpc` | Closed-loop iteration: tag submits, read prior history, repeat `/submit-hpc campaign_id=<slug>` until the strategy stops. See [`docs/workflows/campaign.md`](docs/workflows/campaign.md). |
| `/hpc-axes-init` | Write `<experiment>/.hpc/axes.yaml` with the parallel-axis enumeration + homogeneity hint that drives the cold-start (and warm-path) array-axis picker. |

### Primitives

The slash commands above compose ~50 primitives exposed as `hpc-mapreduce <name>`. Full machine-readable catalog at `docs/generated/operations.md` (auto-regenerated). High-traffic ones for agent orchestration:

| Primitive | Replaces |
|---|---|
| `submit-flow` / `submit-flow-batch` | rsync + deploy + qsub + record (single or N-spec batch with shared rsync). Auto-dispatches when the spec is `{specs: [...]}`. |
| `monitor-flow` | Poll-and-combine loop the slash command's tick body wraps. |
| `aggregate-flow` | rsync_pull `_combiner/` + `reduce_partials` + optional summary pull + ingest runtime samples. |
| `build-submit-spec` | Resolved-interview-values → validated `submit_flow.input.json` spec. |
| `build-tasks-py` | Cartesian-product axes → `.hpc/tasks.py` from the canonical Pattern 1 template. |
| `discover-executors` / `discover-reducers` | Scan repo for executor scripts / aggregator scripts (find existing reducer instead of writing a fresh one). |
| `decide-monitor-arm` | Pick cron/loop/none + cadence + cron schedule + literal `armed:` line. |
| `monitor-summary` | Canonical user-facing tick summary (byte-stable framing). |
| `summarize-submit-plan` | Canonical pre-submit confirmation summary. |
| `verify-canary` | Wait + grep + output-check protocol for 1-task canary submissions. |
| `verify-aggregation-complete` | All-waves-combined / all-tasks-present / no-cross-run-contamination invariant report. |
| `suggest-setup-action` / `find-prior-run` | `/submit-hpc` Setup priority cascade + `cmd_sha` resume detection. |
| `prune-orphan-sidecars` | Clean half-baked sidecars from failed batches. |

`hpc-mapreduce <name> --help` shows the per-primitive args; many take `--spec <path>` for a JSON input. See `docs/primitives/<name>.md` for the per-primitive contract (idempotency, side effects, error codes, schemas).

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

### `~/.claude-hpc/config.json` (optional)

Per-user config for the `recall` primitive's default `--root`. List one or more directories under `experiment_roots` and `recall` walks them all when `--root` is omitted:

```json
{
  "experiment_roots": [
    "/home/user/experiments",
    "/scratch/user/campaigns"
  ]
}
```

The `--root` CLI flag still wins when set. If neither flag nor config is present, `recall` errors with `spec_invalid` rather than silently falling back to cwd.

### Caching

Claude remembers your preferences (cluster, executor directory, environment, resources) across conversations via Claude Code memory. The `.hpc/runs/<run_id>.json` sidecars (paired with `.hpc/tasks.py`) serve as the submission record for monitoring and resubmission.

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
from hpc_mapreduce import (
    # Framework subdirectory layout
    framework_subdir, runs_subdir, tasks_path, load_tasks_module,
    # Per-run sidecars
    compute_cmd_sha, write_run_sidecar, read_run_sidecar,
    find_run_by_cmd_sha, find_existing_runs,
    # Cluster config
    load_clusters_config, get_template_path, _PACKAGE_ROOT,
    # Submission
    ClusterConstraints, parse_constraints,
    WorkloadSpec, compute_submission_plan, build_wave_map,
    deploy_runtime, run_combiner_checked,
)
from hpc_mapreduce.infra.backends import get_backend
```
