# Boundary Contract: claude-hpc ↔ experiment repos

## Purpose

This document is the single source of truth for the boundary between the
`claude-hpc` framework and the experiment repos it orchestrates. It enumerates
(a) the public Python API the framework offers, (b) the filenames the framework
reserves inside experiment repos, (c) the allowed import directions, and (d)
the split between cluster-infrastructure config and per-experiment config. The
allowlist-style lint test in
[`tests/test_boundary_contract.py`](../tests/test_boundary_contract.py)
enforces these lists; any drift fails CI with an actionable diff pointing back
here.

## What claude-hpc owns (public API allowlist)

The exports below are the entire public surface of the `hpc_mapreduce`
package. Groupings mirror those in
[`hpc_mapreduce/__init__.py`](../hpc_mapreduce/__init__.py).

The public boundary also now includes the **shell CLI** at
`hpc_mapreduce/cli.py` (entry point `hpc-mapreduce`). Its envelope
shape, subcommand list, and exit-code contract are documented in
[`docs/cli-spec.md`](cli-spec.md) and the JSON Schemas under
`hpc_mapreduce/schemas/`. The CLI calls into the same atomic-ops layer
(`slash_commands/runner.py`) that the slash commands use, so the
invariants in [`docs/sync-checklist.md`](sync-checklist.md) bind both.

### Package root

- `_PACKAGE_ROOT` — absolute path to the **package directory**
  (`hpc_mapreduce/`, where `__init__.py` lives). Used to resolve
  `hpc_mapreduce/config/clusters.yaml`, the bundled job templates
  under `hpc_mapreduce/templates/<scheduler>/`, and starter templates
  under `hpc_mapreduce/templates/starters/`. Note: this points at the
  package, **not** the repo root.
- `__version__` — package version string, resolved at import time from
  the installed distribution metadata. Falls back to
  `"0.0.0+unknown"` when running from a non-installed checkout.

### Config & discovery

- `load_clusters_config` — parse and return `hpc_mapreduce/config/clusters.yaml`.
- `get_template_path` — resolve a bundled job template by `(scheduler, name)`.

### Remote execution

- `ssh_run` — run a command on a remote cluster.
- `rsync_push` — push a local directory to the cluster.
- `rsync_pull` — pull a remote directory to local.
- `deploy_runtime` — stage the framework's runtime files on the cluster.

### Framework subdirectory layout

Every framework-generated artifact in an experiment repo lives under
`.hpc/`. The only file the user is expected to author there is
`tasks.py`; per-run sidecars in `.hpc/runs/` are generated and
gitignored.

- `HPC_SUBDIR` — `".hpc"`. The directory name itself; reserved (see
  *Reserved filenames* below).
- `TASKS_FILENAME` — `"tasks.py"`. The single file the user owns inside
  `.hpc/`.
- `RUNS_SUBDIR` — `"runs"`. Per-run sidecars live here.
- `framework_subdir` — return `experiment_dir/.hpc`, mkdir it, and
  write `.hpc/.gitignore` (ignoring `runs/`) on first call.
- `runs_subdir` — return `experiment_dir/.hpc/runs`, mkdir it.
- `tasks_path` — return `experiment_dir/.hpc/tasks.py` (does not create).
- `load_tasks_module` — importlib helper that imports a `tasks.py` from
  an arbitrary path; verifies the module exposes `total()` and
  `resolve(task_id)`.

### Per-run sidecars

A sidecar `.hpc/runs/<run_id>.json` carries per-run state for one
submission. Identity fields: `run_id`, `cmd_sha`, `claude_hpc_version`,
`submitted_at`, `executor`, `result_dir_template`, `task_count`,
`tasks_py_sha`, optional `wave_map`. Plus the v2 config snapshot
(populated by `/submit`; absent v2 fields default to `None` on read):
`cluster`, `profile`, `campaign_id`, `project`, `remote_path`,
`resources`, `env`, `env_group`, `constraints`, `gpu_fallback`,
`max_retries`, `runtime`, `auto_retry`, `aggregate_defaults`. v1
sidecars on disk continue to load via `read_run_sidecar`'s backfill.

- `MAX_RUNS` — maximum sidecars retained before pruning (default 500;
  override via `HPC_MAX_RUNS` env var at module load).
- `SIDECAR_SCHEMA_VERSION` — current sidecar schema version (2).
- `compute_cmd_sha` — hash the materialized task list of a `tasks.py`
  module — the source of truth for run identity.
- `compute_tasks_py_sha` — diagnostic hash of `tasks.py`'s bytes.
- `write_run_sidecar` — write `.hpc/runs/<run_id>.json`.
- `read_run_sidecar` — load a sidecar by run_id; backfills v2 keys.
- `find_existing_runs` — list sidecars newest-first.
- `find_run_by_cmd_sha` — locate the newest sidecar matching a cmd_sha.
- `prune_old_runs` — keep only the most recent `MAX_RUNS`.
- `run_sidecar_path` — canonical path for a run_id (does not create).

### Job status & results

- `check_results` — count completed/failed result files for a run.
- `check_results_from_tasks` — same, but driven by a per-task dict
  (synthesized from a per-run sidecar + `.hpc/tasks.py` by
  `_build_per_task_dict_from_sidecar`). Used internally by the
  cluster-side status reporter.
- `report_status` — formatted status report for a submitted job.
- `report_status_from_tasks` — same, but driven by the per-task dict
  synthesized from sidecar + tasks.py. Used by the cluster-side
  reporter and the live TUI.
- `rollup_by_grid_point` — group per-task status into per-grid-point status.
- `detect_scheduler` — identify the scheduler family on a remote host.

### GPU selection

- `pick_gpu` — choose a GPU type given the cluster's `gpu_types` and the
  user's `gpu_fallback` preference list.

### Reduce

- `reduce_metrics` — fold per-task metric files into one record.
- `reduce_by_grid_point` — fold per-task metric files into per-grid-point
  records.
- `reduce_partials` — fold partial / streaming metric files.
- `reduce_resource_usage` — summarise CPU/mem/GPU usage across tasks.
- `classify_failure` — categorise a task failure from its log.

### Closed-loop campaigns

- `hpc_mapreduce.reduce.history.prior(experiment_dir, campaign_id)` —
  read-only per-iteration reduced metrics for a campaign, oldest-first.
  Pure local filesystem walk. Does not import `.hpc/tasks.py`.
- `hpc_mapreduce.reduce.history.find_sidecars_by_campaign` /
  `result_dirs_for_sidecar` — underlying primitives.
- `hpc_mapreduce.campaign.run_campaign` — asyncio in-flight queue (the
  closed-loop driver). Fully IO-injected; user supplies `submit_one`,
  `await_completion`, `should_submit` callbacks.
- **`HPC_CAMPAIGN_ID` env var** — forwarded by every scheduler template
  (SGE/SLURM × CPU/GPU) alongside `HPC_RUN_ID`. Read by the user's
  `tasks.py` and executor on the cluster to call `prior()` for the
  campaign's history. Empty (unset) for open-loop submits.

### Executor discovery

- `ExecutorInfo` — dataclass describing one discovered executor.
- `discover_executors` — scan a directory for executor `.py` files.
- `is_executor_source` — predicate: does this file look like an executor?

### Cluster constraints

- `ClusterConstraints` — typed view of a cluster's throughput limits.
- `parse_constraints` — parse a constraints dict from YAML config.

### Throughput

- `WorkloadSpec` — typed description of a workload (task count, walltime).
- `SubmissionPlan` — output of the throughput optimizer.
- `compute_submission_plan` — derive a `SubmissionPlan` from constraints.
- `build_wave_map` — assign each task to a wave for staggered submission.

### Resubmit

- `compact_task_ids` — collapse a task-id list into scheduler array syntax.
- `ResubmitBatch` — one batch in a resubmit plan.
- `ResubmitPlan` — full plan for resubmitting failed/missing tasks.
- `resubmit_plan` — build a `ResubmitPlan` from a `task_count` plus a list of `failed_task_ids`.

### Combiner

- `run_combiner` — execute the combiner step on the cluster.
- `run_combiner_checked` — same, with explicit error reporting.

### Per-task metrics sidecar

- `write_metrics` — helper executors call to write a sidecar metrics file
  into `$RESULT_DIR`.

### Default reduce semantics

The exports under **Reduce**, **Combiner**, and **Per-task metrics sidecar**
above provide a turnkey default for the cluster-side data-locality path
(see `commands/aggregate.md`): executors call `write_metrics({...})` per
task, the cluster-deployed `_hpc_combiner.py` aggregates those sidecars by
grid point using weighted mean (weight = `n_samples`, default 1) with
Neumaier-compensated summation for order-invariance.

The choice of weighted-mean reduction is a *framework default*, not a
contract requirement. Experiment repos that need a different reduce shape
(median, max, percentile, custom) can opt out today by **skipping
`write_metrics` entirely**: emit results to `--output-file`, let
`/aggregate` rsync the raw outputs back, and run reduction locally with
whatever tools fit. The combiner runs on the cluster only when a task has
written a `metrics.json` sidecar, so an experiment with no sidecars simply
gets no cluster-side reduction.

A general user-supplied reduce hook (where the cluster-deployed combiner
discovers and `runpy`-imports a reduce callable from the experiment repo)
is *not* implemented today. The local `run_combiner` API cannot pass a
Python callable to the cluster-side process, and `_hpc_combiner.py` is in
the reserved-filenames list — `deploy_runtime` always overwrites it with
the framework default. Adding the hook would require a discovery
convention plus tests, and should land when an experiment actually needs
it. Until then, "skip `write_metrics`" is the supported override path.

## What experiment repos own

Everything outside the framework's public API. Concretely:

- **Executor scripts** under `executors/` or `src/` (any `.py` file with a
  CLI and an `if __name__ == "__main__":` guard — see `discover.py`).
- **Shared utility code** under `lib/` (or wherever the experiment chooses to
  put it).
- **`.hpc/tasks.py`** — the user-written Python module exposing
  `total()` and `resolve(task_id)`. Authored once via `/submit`
  Step 6's scaffolding flow (adapting the canonical example at
  `hpc_mapreduce/templates/tasks_example.py`), git-tracked, and
  user-editable. The bridge between the framework's task-id contract
  and whatever parallelization axis the experiment needs.
- **`.hpc/stages.py`** (optional) — the user-written Python module
  exposing `stages() -> list[dict]` for multi-stage DAG submissions.
  Validated against `hpc_mapreduce/schemas/stages.input.json` at load
  time. Same conversational-generation pattern as `.hpc/tasks.py`.
- **Domain-specific aggregation** — any `aggregate_cmd` the experiment
  defines for fan-in.

All framework-generated artifacts live under the **reserved
directory `.hpc/`**:

- `.hpc/tasks.py` — user-authored, git-tracked, the only file the user
  edits in there.
- `.hpc/runs/<run_id>.json` — per-run sidecar (gitignored).
- `.hpc/_hpc_dispatch.py`, `.hpc/_hpc_combiner.py`,
  `.hpc/templates/{cpu,gpu}_array.{sh,slurm}` — placed on the **cluster**
  copy of `.hpc/` by `deploy_runtime`. They are not present in the
  local `.hpc/` and are protected from rsync `--delete` by
  `DEFAULT_RSYNC_EXCLUDES`.

## Reserved directories

The framework reserves the **`.hpc/` directory** inside experiment
repos. The discovery scanner skips this directory wholesale via
`_SKIP_DIRS` in
[`hpc_mapreduce/job/discover.py`](../hpc_mapreduce/job/discover.py), so
nothing inside it is misclassified as an executor. Experiment authors
must not place user-code files (executors, libraries) under `.hpc/`.

`_SKIP_BASENAMES` retains `__init__.py` so package markers are not
treated as runnable executors. That is a Python package convention, not
a framework reservation; experiment repos may use `__init__.py` freely.

## Import directions (allowed)

1. **`hpc_mapreduce/**` may import the standard library plus the
   third-party deps listed in `pyproject.toml`** (currently just `pyyaml`).
   No other runtime deps.
2. **`hpc_mapreduce/**` MUST NOT import from
   `hpc_mapreduce/templates/`.** Templates are source files copied
   into experiment repos; treating them as importable modules would
   couple the framework to a fixed set of templates.
3. **`hpc_mapreduce/templates/**` MUST NOT import from
   `hpc_mapreduce/**`** — with one narrow exception. Templates ship
   into experiment repos and run there, where `claude-hpc` is
   generally not installed. The exception: a small allowlist of
   "runtime modules" that `deploy_runtime`
   (`hpc_mapreduce/infra/remote.py`) explicitly copies onto the cluster
   compute node alongside the executor. Templates may import from those
   because they are guaranteed to be present at execution time. The current
   allowlist (kept in sync with `RUNTIME_MODULES_ALLOWED_IN_TEMPLATES` in
   `tests/test_boundary_contract.py`) is:
   - `hpc_mapreduce.map.metrics_io` — the `write_metrics` sidecar writer.
     Stdlib-only, deployed alongside `combiner.py`. Templates use a lazy
     import gated on `$RESULT_DIR` so smoke tests still run without
     `claude-hpc` installed.

   To add a new entry, the module must (a) be deployed by `deploy_runtime`,
   (b) be stdlib-only or self-contained, and (c) be added to both the
   allowlist constant in the lint test and this doc in the same PR.
4. **`tests/**` may import either** — tests live in the framework repo and
   exercise both sides.

## Config split

Cluster-infrastructure config and per-experiment config are deliberately
separated, so that adding a new experiment never requires editing the
framework, and adding a new cluster never requires touching any experiment.

- **`hpc_mapreduce/config/clusters.yaml`** — cluster infrastructure
  (host, scheduler, scratch path, modules, conda envs, GPU types,
  throughput constraints). Ships with `claude-hpc`. See
  [`README.md`](../README.md).
- **Per-run sidecars at `.hpc/runs/<run_id>.json`** — the v2 schema
  captures the full per-experiment config snapshot (resources, env,
  constraints, profile name, runtime, auto_retry, aggregate defaults)
  for each successful submit. Subsequent commands read it instead of a
  separate experiment-config file. Conversational `/submit` writes one;
  there is no user-authored experiment-config yaml.

The lint test `test_clusters_yaml_is_infra_only` enforces that
`hpc_mapreduce/config/clusters.yaml` only contains infrastructure-shaped
keys; any experiment-shaped field (e.g. `grid`, `executors`) leaking
into a cluster entry will fail it.

## How to extend

When adding a new public export, a new reserved filename, or a new template,
update **this document and `tests/test_boundary_contract.py`** in the same
PR as the code change. The lint test compares its allowlist constants
against the live module attributes; any unannounced addition or removal will
fail with a diff that points back here.

Specifically:

- New public export → add it to `__all__` in
  `hpc_mapreduce/__init__.py`, list it in the appropriate section above,
  and add it to `ALLOWED_EXPORTS` in the test.
- New reserved filename → add it to `_SKIP_BASENAMES` in
  `hpc_mapreduce/job/discover.py`, list it under "Reserved filenames"
  above, and add it to `RESERVED_FILES` in the test.
- New cluster-config key → add it to `ALLOWED_CLUSTER_KEYS` in the test
  and document it under "Config split" (and in `docs/schema.md` if
  user-facing).
