# Configuration Precedence

Both surfaces (slash commands and the `hpc-mapreduce` CLI) resolve
configuration through the same precedence order. Anywhere a value can
be sourced from more than one place, the highest-priority source wins
and lower-priority values are ignored — never merged field-wise unless
explicitly noted.

## Precedence (highest wins)

```
1. Explicit CLI flag / function kwarg
2. --spec spec.json field (CLI) or interactive answer (slash command)
3. Most recent matching `.hpc/runs/<run_id>.json` sidecar (per-experiment, per-profile)
4. Env var (HPC_JOURNAL_DIR, HPC_CLUSTERS_CONFIG, ...)
5. Built-in default (clusters.yaml shipped in the package)
```

Notes:

- Layers 1–2 are *call-site* config. They override anything persisted.
- Layer 3 (per-run sidecars) is *repo-level* persistence written by
  every successful `/submit`. The v2 schema captures resources, env,
  constraints, profile name, runtime, auto_retry, and aggregate defaults
  so subsequent commands rebuild full context without an external config
  file. Conversational generation only — there is no user-authored
  experiment-config yaml.
- Layer 4 (env vars) is *operator* config. Used by MARs and CI to point
  the framework at non-default state directories or alternate cluster
  catalogs.
- Layer 5 (`claude_hpc/config/clusters.yaml`) is the *package
  default*. Ships inside the wheel; only edited via PR.

## Where each kind of config is consumed

### Cluster definitions

- **Source of truth**: `claude_hpc/config/clusters.yaml` (shipped
  with the package).
- **Loader**: `claude_hpc.infra.clusters.load_clusters_config` —
  re-exported as `claude_hpc.load_clusters_config`.
- **Override path**: set `HPC_CLUSTERS_CONFIG=/path/to/clusters.yaml`
  to redirect the loader at an alternate file. (CLI flag override is
  not currently exposed; an MAR running against a fork drops a sibling
  YAML in place and points the env var at it.)
- **Schema**: documented in `docs/boundary-contract.md` under "Config
  split"; the lint test `test_clusters_yaml_is_infra_only` enforces
  infra-only keys.

### Journal directory (per-run state)

- **Default**: `~/.claude/hpc/`
- **Override**: env var `HPC_JOURNAL_DIR=/some/dir`. Read at import
  time in `slash_commands/session.py:HPC_HOMEDIR`. MARs that want
  isolated state per agent set this to a per-agent path.
- **No CLI flag, no sidecar field** — journal location is operator
  config, not experiment config.

### Executor discovery

- **Source**: `claude_hpc.orchestrator.discover.discover_executors` walks
  `--experiment-dir` (CLI) or the active experiment repo (slash).
- **Reserved directory** (skipped wholesale): `.hpc/` — see `_SKIP_DIRS`
  in `claude_hpc/orchestrator/discover.py`. The framework files inside it
  (`tasks.py`, `runs/<run_id>.json`, and on the cluster also
  `_hpc_dispatch.py`, `_hpc_combiner.py`, `templates/`) are never
  treated as user executors.
- **Reserved filenames**: `__init__.py` (Python convention; same
  `_SKIP_BASENAMES`).
- **Override path**: there is no config file for this; experiment
  authors influence discovery by where they put their `.py` files. The
  `hpc-mapreduce discover` subcommand exposes the result for inspection.

### Parallelization axis (`.hpc/tasks.py`)

- **Layer 1**: `.hpc/tasks.py` itself — a user-written Python module
  exposing `total()` and `resolve(task_id)`. Authored once via
  `/submit` Step 6's scaffolding flow; thereafter committed to git and
  reused on every submit.
- **No env override** — the axis is part of the experiment's source
  code, not its environment.
- Cartesian products, chunking, etc. are the user's responsibility —
  write `itertools.product`, slicing, or whatever shape fits inside
  `.hpc/tasks.py`. The framework provides no axis primitives.

### Resource overrides (mem, walltime, gpus, gpu_type)

- **Layer 1**: `--spec.overrides` on `hpc-mapreduce resubmit`, or an
  interactive answer in `/submit`.
- **Layer 3**: most recent matching run sidecar's `resources` block —
  the resolved values from a prior submit, available for reuse.
- **Layer 5**: cluster-level defaults baked into `clusters.yaml` (e.g.
  scheduler-specific module loads, default GPU types). Sidecar-level
  resources do not inherit field-by-field from the cluster — only
  `constraints` (throughput limits) merge field-wise per the documented
  behavior in `parse_constraints`.

### Cluster constraints (throughput optimizer inputs)

- **Layer 3**: most recent matching run sidecar's `constraints` block
  (per-experiment override resolved at the prior submit).
- **Layer 5**: `clusters.yaml`'s `constraints:` block (cluster-level
  default).
- Sidecar-level keys override cluster-level keys **field-by-field**;
  unset sidecar keys fall back to the cluster default. Implemented in
  `claude_hpc.orchestrator.constraints.parse_constraints`.

## Env vars consumed

| Var | Default | Read by | Effect |
|---|---|---|---|
| `HPC_JOURNAL_DIR` | `~/.claude/hpc` | `slash_commands/session.py` | Redirect journal storage. |
| `HPC_CLUSTERS_CONFIG` | (package default) | `claude_hpc/infra/clusters.py` | Use alternate `clusters.yaml`. |
| `HPC_NO_SSH_MULTIPLEX` | unset | `claude_hpc/agent_cli.py:cmd_capabilities` | When `1`, disables SSH ControlMaster reuse; surfaced in `capabilities.data.ssh_multiplexing`. |
| `SSH_AUTH_SOCK` | (set by ssh-agent) | `cmd_preflight` | Required for SSH auth; preflight fails if missing. |
| `HPC_MAX_RUNS` | `500` | `claude_hpc/orchestrator/runs.py` | Override the per-experiment cap on retained run sidecars. |
| `HPC_RUN_ID` | (none, required) | cluster-side `.hpc/_hpc_dispatch.py`, `.hpc/_hpc_combiner.py` | Locates `.hpc/runs/<run_id>.json`. |
| `HPC_TASK_ID` | (none, required) | cluster-side `.hpc/_hpc_dispatch.py` | 0-based task index. `TASK_ID` is accepted as a fallback for the env-var transition. |
| `HPC_TASKS_PATH` | sibling of `_hpc_dispatch.py` | cluster-side `.hpc/_hpc_dispatch.py` | Override path to user's `tasks.py`. |
| `HPC_CAMPAIGN_ID` | unset | scheduler templates → cluster-side dispatcher → user `tasks.py` | When set, marks the run as part of a closed-loop campaign. The user's `tasks.py` calls `claude_hpc.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` to get prior iterations' reduced metrics. |
| `HPC_WAVE` | (none) | cluster-side `.hpc/_hpc_combiner.py` | Wave index when `--wave` is absent. |
| `HPC_RUNTIME` | unset | scheduler templates | When `uv`, the template runs `uv sync` before dispatch. |

## Versioning

Adding a new env var, changing a precedence rule, or moving a value
from one layer to another is a **breaking change**. Bump the package
version and update this doc plus `docs/sync-checklist.md` in the same
PR.
