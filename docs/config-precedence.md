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
3. hpc.yaml in the experiment dir
4. Env var (HPC_JOURNAL_DIR, HPC_CLUSTERS_CONFIG, ...)
5. Built-in default (clusters.yaml shipped in the package)
```

Notes:

- Layers 1–2 are *call-site* config. They override anything persisted.
- Layer 3 (`hpc.yaml`) is *repo-level* config. Lives in the experiment
  repo, versioned alongside the experiment code.
- Layer 4 (env vars) is *operator* config. Used by MARs and CI to point
  the framework at non-default state directories or alternate cluster
  catalogs.
- Layer 5 (`hpc_mapreduce/config/clusters.yaml`) is the *package
  default*. Ships inside the wheel; only edited via PR.

## Where each kind of config is consumed

### Cluster definitions

- **Source of truth**: `hpc_mapreduce/config/clusters.yaml` (shipped
  with the package).
- **Loader**: `hpc_mapreduce.infra.clusters.load_clusters_config` —
  re-exported as `hpc_mapreduce.load_clusters_config`.
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
- **No CLI flag, no `hpc.yaml` field** — journal location is operator
  config, not experiment config.

### Executor discovery

- **Source**: `hpc_mapreduce.job.discover.discover_executors` walks
  `--experiment-dir` (CLI) or the active experiment repo (slash).
- **Reserved filenames** (skipped): `_hpc_dispatch.py`,
  `_hpc_combiner.py`, `hpc_chunking_shim.py`, `__init__.py` — see
  `_SKIP_BASENAMES` in `hpc_mapreduce/job/discover.py`.
- **Override path**: there is no `hpc.yaml` field for this; experiment
  authors influence discovery by where they put their `.py` files. The
  `hpc-mapreduce discover` subcommand exposes the result for inspection.

### Grid params

- **Layer 1**: `expand-grid --spec spec.json` (CLI, `{"grid": {...}}`).
- **Layer 2**: `hpc.yaml` profile's `grid:` block (see
  `docs/schema.md`). Slash `/submit` reads this and offers it as a
  pre-populated answer; the user can override interactively at submit
  time.
- **No env override** — grids are experiment-level.
- Cartesian-product expansion happens in `hpc_mapreduce.job.grid.expand_grid`.

### Resource overrides (mem, walltime, gpus, gpu_type)

- **Layer 1**: `--spec.overrides` on `hpc-mapreduce resubmit`, or an
  interactive answer in `/submit`.
- **Layer 2**: `hpc.yaml` profile's `resources:` block (see
  `docs/schema.md`).
- **Layer 5**: cluster-level defaults baked into `clusters.yaml` (e.g.
  scheduler-specific module loads, default GPU types). Profile-level
  resources do not inherit field-by-field from the cluster — only
  `constraints` (throughput limits) merge field-wise per `hpc.yaml`'s
  documented behavior.

### Cluster constraints (throughput optimizer inputs)

- **Layer 2**: `hpc.yaml` profile's `constraints:` block (per-experiment
  override).
- **Layer 5**: `clusters.yaml`'s `constraints:` block (cluster-level
  default).
- Profile-level keys override cluster-level keys **field-by-field**;
  unset profile keys fall back to the cluster default. Implemented in
  `hpc_mapreduce.job.constraints.parse_constraints`.

## Env vars consumed

| Var | Default | Read by | Effect |
|---|---|---|---|
| `HPC_JOURNAL_DIR` | `~/.claude/hpc` | `slash_commands/session.py` | Redirect journal storage. |
| `HPC_CLUSTERS_CONFIG` | (package default) | `hpc_mapreduce/infra/clusters.py` | Use alternate `clusters.yaml`. |
| `HPC_NO_SSH_MULTIPLEX` | unset | `hpc_mapreduce/cli.py:cmd_capabilities` | When `1`, disables SSH ControlMaster reuse; surfaced in `capabilities.data.ssh_multiplexing`. |
| `SSH_AUTH_SOCK` | (set by ssh-agent) | `cmd_preflight` | Required for SSH auth; preflight fails if missing. |
| `HPC_MANIFEST` | `_hpc_dispatch.json` | cluster-side `_hpc_dispatch.py`, `_hpc_combiner.py` | Manifest filename override on the compute node. |
| `HPC_WAVE` | (none) | cluster-side `_hpc_combiner.py` | Wave index when args absent. |
| `TASK_ID` | (none) | cluster-side `_hpc_dispatch.py` | 1-based task index. |

## Versioning

Adding a new env var, changing a precedence rule, or moving a value
from one layer to another is a **breaking change**. Bump the package
version and update this doc plus `docs/sync-checklist.md` in the same
PR.
