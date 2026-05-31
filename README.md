# hpc-agent

HPC orchestrator for array-batch experiments on SGE/SLURM clusters. Two surfaces over one core:

- **Slash commands for humans** in Claude Code (`/submit-hpc`, `/monitor-hpc`, `/aggregate-hpc`, `/campaign-hpc`) — interactive markdown templates in `slash_commands/commands/*.md` that walk you through choosing a cluster and authoring `.hpc/tasks.py`. The four workflow triggers cover every end-user moment; entry-point onboarding, axis classification, and axes-init are folded into `/submit-hpc`'s escalation playbook (the worker escalates when it can't proceed; the playbook walks the user through the dialog and the agent invokes the relevant skill with a resolved spec). Environment preflight (SSH agent, cluster reachability) is a one-time-per-machine CLI step: `hpc-agent setup --cluster <name>` probes the cluster and writes a 24h cache marker `/submit-hpc`'s Step 6b gate reads — runtime workflows assume setup succeeded.
- **CLI for agents and automation** (`hpc-agent <subcommand>`) — JSON-in, JSON-out, exit codes. Designed to be invoked via a `Bash`-style tool by external orchestrators. This is a POSIX-native agent surface: any tool that can shell out and parse JSON can drive a cluster — see [`docs/reference/agent-surface.md`](docs/reference/agent-surface.md). For integrators: [`docs/integrations/CONTRACT.md`](docs/integrations/CONTRACT.md).

Both surfaces invoke `hpc-agent <subcommand>`. The slash commands are pure markdown that orchestrate the binary; the binary's atomic-ops layer (the per-subject runners under `hpc_agent/ops/`) ensures cross-surface state — in-flight runs, journal records under `~/.claude/hpc/<repo_hash>/` — is shared automatically.

## Quick Start

### For humans (Claude Code)

```bash
pip install hpc-agent                              # or `pip install -e .` from a checkout
hpc-agent setup                                    # copy commands + skills
hpc-agent setup --cluster hoffman2                 # probe cluster + populate 24h preflight cache (run once per cluster)
```
`hpc-agent setup` (no flags) copies the bundled slash commands into
`~/.claude/commands/` and the skills into `~/.claude/skills/` —
idempotent. Re-run with `--cluster <name>` once per machine + cluster
to probe SSH agent reachability, ssh/transport on PATH,
`clusters.yaml` parseability, and TCP :22; on a green probe it writes
a 24h cache marker that `/submit-hpc`'s Step 6b gate consults, so the
first submit doesn't re-run the check. Pass `--dry-run` to preview.
Each preflight check's `detail` field carries actionable remediation
prose, so a red probe tells you exactly what to fix. Every command
(`/submit-hpc`, `/monitor-hpc`, `/aggregate-hpc`, `/campaign-hpc`)
and skill ships inside the package.

Once installed:

- `hpc-agent setup --cluster <name>` (once per machine + cluster) — install assets and probe each cluster you'll submit to. Runtime workflows assume setup succeeded; re-run if SSH credentials or a cluster's reachability change.
- `/submit-hpc` — answer prompts about cluster, executor, grid params. The worker escalates with structured intent prompts (entry-point onboarding, axis classification) when it can't proceed; the in-chat agent walks the user through the escalation playbook and invokes the relevant skill with the resolved spec.
- `/monitor-hpc` to monitor, `/aggregate-hpc` to collect results.

### Authentication

The workflow slash commands run their multi-step work in a fresh-context
`claude -p --bare` worker. **That worker authenticates only via
`ANTHROPIC_API_KEY` (or cloud-provider credentials) — it cannot use a Claude
Code OAuth/subscription login.** If your session is OAuth-authenticated, export
an API key before launching; otherwise the worker fails fast with a clear
`worker authentication unavailable` error rather than an opaque "Not logged in":

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Permissions:** the bare worker is headless — it can't answer a permission
prompt. It needs an allow rule for the `hpc-agent` CLI. `hpc-agent interview`
(onboarding) writes a project-scoped `<experiment-dir>/.claude/settings.json`
granting `Bash(hpc-agent:*)`, so launching `claude` from the experiment dir
just works. If you launch `claude` from elsewhere, add the rule to your
user-global `~/.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(hpc-agent:*)"] } }
```

### For agents and automation

```bash
pip install hpc-agent
hpc-agent setup --cluster hoffman2                        # one-time: install assets + probe cluster + write 24h cache marker
hpc-agent interview --spec intent.json --campaign-dir <d> # persist campaign intent next to tasks.py
hpc-agent recall --root ~/experiments --task-kind <kind>  # query past interviews for next-interview grounding
hpc-agent submit --spec spec.json                          # JSON envelope on stdout
hpc-agent status --run-id <id>                             # one-shot snapshot; poll as needed
hpc-agent aggregate --run-id <id> --wave 1                 # combiner + result pull
```
Stdout is a single-line JSON envelope: `{"ok": true, "idempotent": ..., "data": {...}}` or `{"ok": false, "error_code": ..., "retry_safe": ..., "remediation": ...}`. Exit codes: 0 ok, 1 user error, 2 cluster/network, 3 internal. Full schema in [`docs/reference/cli-spec.md`](docs/reference/cli-spec.md); JSON Schema files for runtime validation under `hpc_agent/schemas/`.

### For integrators

hpc-agent is `Bash`-invokable from any agent harness with a JSON
parser. See **[`docs/integrations/CONTRACT.md`](docs/integrations/CONTRACT.md)**
for the full contract: the spawn env block,
`error_code` → retry policy table, the `find-prior-run` → `submit` →
`monitor-summary` → `verify-aggregation-complete` workflow, the
`.hpc/tasks.py` boundary, and the executor import allowlist.

The canonical reference for `.hpc/tasks.py` is shipped inside the
package at
[`src/hpc_agent/models/mapreduce/templates/scaffolds/tasks_example.py`](src/hpc_agent/models/mapreduce/templates/scaffolds/tasks_example.py).
It demonstrates three patterns (Cartesian product, chunking by row
count, date-window backtests) inline. Integrators locate it at runtime
via `from hpc_agent import _PACKAGE_ROOT` or `rglob("tasks_example.py")`.

The most common first-time failure is the harness's default-empty
spawn env dropping `SSH_AUTH_SOCK`. `hpc-agent
status`/`aggregate`/`reconcile` fail fast with `error_code:
"ssh_unreachable"` (exit 2) instead of hanging on auth — run
`hpc-agent setup --cluster <name>` once on each machine to verify the
spawn env and populate the 24h cache marker that `/submit-hpc`'s
Step 6b gate reads. hpc-agent does not kill cluster jobs by design
(`settings.json` denies `scancel`/`qdel`); if the integrator decides
a run is bad, stop polling and let it expire.

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
hpc-agent setup --cluster <name> → one-time per machine: install assets + probe cluster
/submit-hpc                      → discovers executors, walks you through .hpc/tasks.py, syncs code, submits
/monitor-hpc                     → tracks completion per grid point, diagnoses failures, auto-resubmits
/aggregate-hpc                   → validates completeness, runs aggregation, downloads summaries
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

The framework's contract with your experiment is a `@register_run`-decorated Python function with typed kwargs — that function can live in a notebook, a `.py` script, or a package module; `discover_runs` AST-walks all three indifferently. See [`docs/internals/experiment-contract.md`](docs/internals/experiment-contract.md) for the canonical description.

The boundary between hpc-agent and your experiment repo is documented in [`docs/reference/boundary-contract.md`](docs/reference/boundary-contract.md) and enforced by `tests/test_boundary_contract.py`.

1. Claude reads your executor scripts and their `--help` output.
2. You describe what to run in natural language — Claude walks you through writing `.hpc/tasks.py` once: a small Python module exposing `total()` and `resolve(task_id)` that returns the per-task kwargs. The file is committed to git and reused on every subsequent submit.
3. A per-run sidecar `.hpc/runs/<run_id>.json` records the executor command, result-dir template, `cmd_sha`, and wave map for this particular submission.
4. The framework executor `_hpc_dispatch.py` (zero deps, stdlib-only) is deployed to the cluster's `.hpc/` by `deploy_runtime`.
5. The job template runs the dispatcher, which imports your `.hpc/tasks.py`, calls `resolve(task_id)`, formats the result_dir, and execs your executor command with kwargs as env vars.
6. Your executor reads kwargs as ordinary env vars (uppercased + `HPC_KW_*`) — no HPC awareness needed.

### Parallelism Model

The parallelization axis lives entirely in user code (`.hpc/tasks.py`). The framework is agnostic to whether you're doing a Cartesian grid, chunking by row count, date-window backtests, or something else — it just calls `total()` and `resolve(i)`. The canonical reference at `hpc_agent/models/mapreduce/templates/scaffolds/tasks_example.py` shows three patterns inline; the agent helps you keep whichever applies and delete the rest.

### Memory across campaigns

Two primitives — `interview` and `recall` — close the loop between consecutive campaigns. The interview agent (Claude Code or any external orchestrator) persists structured intent (`goal`, `task_count`, `budget`, `abort_if`, `task_generator`, `cluster_target`, `transcript`, provenance) into `<campaign_dir>/interview.json` next to the materialized `tasks.py`. The next interview calls `recall --root <experiments-dir>` to query past intents, returning recency-sorted summaries plus a 3-tier rollup (counts/histograms/quantiles, optional walltime aggregation, optional per-generator parameter envelopes). Observed ranges only — reasoning over them stays in the calling agent.

See [`docs/workflows/memory-across-campaigns.md`](docs/workflows/memory-across-campaigns.md) for the full flow, including the `task_generator` typed materializer (5 shapes: `enumerated`, `cartesian_product`, `items_x_seeds`, `numeric_logspace`, `numeric_linspace`) and the `~/.hpc-agent/config.json:experiment_roots` default-root config.

### Throughput Optimization

hpc-agent automatically optimizes job submissions for cluster constraints. When constraints are configured (max array size, walltime, concurrent job limits), the optimizer packs tasks into batched waves:

- Tasks are split into arrays of ≤max_array_size
- Arrays are grouped into waves of ≤max_concurrent_jobs
- Waves are staggered via scheduler dependencies (SLURM `--dependency`, SGE `-hold_jid`)
- Total wall-clock time is estimated when per-task duration is known

Configure constraints in `clusters.yaml` (cluster-level); per-experiment overrides resolved at `/submit` time are persisted to the run sidecar at `.hpc/runs/<run_id>.json`.

## Commands

| Command | What it does |
|---------|-------------|
| `/submit-hpc` | Discover executors (scaffolds inline if none found), build grid conversationally, write `.hpc/tasks.py` with FLAGS dict + `.hpc/cli.py` dispatcher, sync code, submit array jobs. Carries an escalation playbook covering entry-point onboarding, axis classification, and axes-init dialogs. |
| `/monitor-hpc` | Poll status, diagnose failures, auto-resubmit, self-schedule next check |
| `/aggregate-hpc` | Validate completeness, run aggregation on cluster, download summaries |
| `/campaign-hpc` | Closed-loop iteration: tag submits, read prior history, repeat `/submit-hpc campaign_id=<slug>` until the strategy stops. Carries the validate-campaign findings interpretation guide. See [`docs/workflows/campaign.md`](docs/workflows/campaign.md). |

Setup is a CLI step, not a slash: run `hpc-agent setup --cluster <name>` once per machine + cluster (see Quick Start above). Each preflight check's `detail` field carries actionable remediation prose.

### Primitives

The slash commands above compose ~50 primitives exposed as `hpc-agent <name>`. Full machine-readable catalog at `docs/generated/operations.md` (auto-regenerated). High-traffic ones for agent orchestration:

| Primitive | Replaces |
|---|---|
| `submit-flow` / `submit-flow-batch` | rsync + deploy + qsub + record (single or N-spec batch with shared rsync). Auto-dispatches when the spec is `{specs: [...]}`. |
| `monitor-flow` | Poll-and-combine loop the slash command's tick body wraps. |
| `aggregate-flow` | rsync_pull `_combiner/` + `reduce_partials` + optional summary pull + ingest runtime samples. |
| `build-submit-spec` | Resolved-interview-values → validated `submit_flow.input.json` spec. |
| `build-tasks-py` | Cartesian-product axes → `.hpc/tasks.py` from the canonical Pattern 1 template. |
| `discover-executors` / `discover-reducers` | Scan repo for executor scripts / aggregator scripts (find existing reducer instead of writing a fresh one). |
| `decide-monitor-arm` | Pick cron/loop/none + cadence + cron schedule for scheduling the next monitor tick. |
| `monitor-summary` | Canonical user-facing tick summary (byte-stable framing). |
| `summarize-submit-plan` | Canonical pre-submit confirmation summary. |
| `verify-canary` | Wait + grep + output-check protocol for 1-task canary submissions. |
| `verify-aggregation-complete` | All-waves-combined / all-tasks-present / no-cross-run-contamination invariant report. |
| `suggest-setup-action` / `find-prior-run` | `/submit-hpc` Setup priority cascade + `cmd_sha` resume detection. |
| `prune-orphan-sidecars` | Clean half-baked sidecars from failed batches. |

`hpc-agent <name> --help` shows the per-primitive args; many take `--spec <path>` for a JSON input. See `docs/primitives/<name>.md` for the per-primitive contract (idempotency, side effects, error codes, schemas).

## Configuration

### `clusters.yaml` (required)

Cluster infrastructure definitions. Ships inside the package at `hpc_agent/config/clusters.yaml`. Override the active path with `HPC_CLUSTERS_CONFIG=/your/clusters.yaml` (useful for integrators who want to keep their cluster definitions outside the package):

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

### `~/.hpc-agent/config.json` (optional)

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
| CPU array | `hpc_agent/models/mapreduce/templates/runtime/sge/cpu_array.sh` | `hpc_agent/models/mapreduce/templates/runtime/slurm/cpu_array.slurm` |
| GPU array | `hpc_agent/models/mapreduce/templates/runtime/sge/gpu_array.sh` | `hpc_agent/models/mapreduce/templates/runtime/slurm/gpu_array.slurm` |

Templates are parameterized via environment variables injected at submission time. Resolve paths via `hpc_agent.get_template_path(scheduler, template)`. The GPU template is used when the configured resources include `gpus`; otherwise the CPU template is used.

## Supported Clusters

| Cluster | Institution | Scheduler |
|---------|------------|-----------|
| Hoffman2 | UCLA IDRE | SGE |
| Discovery | USC CARC | SLURM |

Cluster connection details are in `hpc_agent/config/clusters.yaml` (or whatever `HPC_CLUSTERS_CONFIG` points at).

## Python API

**Prefer the CLI verbs.** Anything an agent or workflow does — compute a run
id, find a prior run, write a sidecar, plan throughput, submit, aggregate —
has a `hpc-agent <verb>` primitive with a validated JSON contract (`hpc-agent
capabilities`, then `hpc-agent describe <name>`). Drive a cluster through those,
not by importing internals and re-implementing a workflow step: the primitives
carry the idempotency, schema validation, and journal/dedup guarantees that
bare functions don't. The headless worker is in fact restricted to the CLI.

### Library surface (standalone, non-agent use)

When you're embedding hpc-agent as a Python library rather than driving it as
an agent, these are the stable, intended imports:

```python
# Framework subdirectory layout
from hpc_agent import RUNS_SUBDIR, RepoLayout, TASKS_FILENAME, load_tasks_module

# Cluster config + templates
from hpc_agent import _PACKAGE_ROOT, get_template_path, load_clusters_config

# Submission planning
from hpc_agent.infra.constraints import ClusterConstraints, parse_constraints
from hpc_agent.infra.throughput import (
    WorkloadSpec,
    build_wave_map,
    compute_submission_plan,
)

# Remote execution + backends
from hpc_agent.infra.backends import get_backend
from hpc_agent.infra.remote import deploy_runtime
```

### Primitive-backing internals — import only to read, not to re-implement

These functions *are* the internals that back CLI verbs; they remain importable
for inspection and tests, but reaching for them to reproduce a workflow step is
the freestyle path the CLI exists to replace. Each maps to a verb — use the verb:

| Internal | Use this verb instead |
|---|---|
| `hpc_agent.state.run_sha.compute_cmd_sha` | `hpc-agent compute-run-id` |
| `hpc_agent.state.runs.find_run_by_cmd_sha` / `find_existing_runs` | `hpc-agent find-prior-run` |
| `hpc_agent.state.runs.write_run_sidecar` | `hpc-agent write-run-sidecar` |
| `hpc_agent.state.runs.read_run_sidecar` | `hpc-agent load-context` (envelope carries run state) |

## Development

```bash
pip install -e '.[dev]'
pre-commit install        # auto-runs ruff, frontmatter regen, index regen
pytest -q                 # 1400+ tests
```

The pre-commit hook regenerates `docs/primitives/*.md` frontmatter,
`docs/primitives/README.md` catalog, and `docs/generated/operations.md`
from the `@primitive` registry, then auto-stages the result. Without it
you'll see CI fail on the corresponding `--check` gates and have to
push a follow-up `chore: regenerate ...` commit.

