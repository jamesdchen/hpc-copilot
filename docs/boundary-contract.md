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

### Package root

- `_PACKAGE_ROOT` — absolute path to the framework checkout; used to resolve
  `config/clusters.yaml` and bundled templates.

### Config & discovery

- `load_clusters_config` — parse and return `config/clusters.yaml`.
- `get_template_path` — resolve a bundled job template by `(scheduler, name)`.

### Remote execution

- `ssh_run` — run a command on a remote cluster.
- `rsync_push` — push a local directory to the cluster.
- `rsync_pull` — pull a remote directory to local.
- `deploy_runtime` — stage the framework's runtime files on the cluster.

### Job status & results

- `check_results` — count completed/failed result files for a run.
- `check_results_from_manifest` — same, keyed off a dispatch manifest.
- `report_status` — formatted status report for a submitted job.
- `report_status_from_manifest` — manifest-driven variant.
- `rollup_by_grid_point` — group per-task status into per-grid-point status.
- `detect_scheduler` — identify the scheduler family on a remote host.

### Shim cache

- `shim_cache_key` — hash of the inputs that uniquely identify a shim.
- `load_cached_shim` — fetch a previously-generated shim from the cache.
- `save_shim` — store a generated shim under its cache key.

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

### Grid API

- `expand_grid` — Cartesian product of a grid spec into per-task parameter
  dicts.
- `build_task_manifest` — produce the `_hpc_dispatch.json` manifest for a run.
- `total_tasks` — count tasks in a grid spec.
- `attach_wave_map` — annotate a manifest with throughput-optimizer waves.
- `MANIFEST_SCHEMA_VERSION` — current schema version of the manifest format.
- `resolve_git_sha` — short git SHA of the experiment repo (or `"nogit"`).
- `validate_result_dir_template` — check `results.dir` placeholders resolve.

### Manifest filenames & resume

- `MAX_MANIFESTS` — maximum kept manifests before pruning.
- `MANIFEST_ALIAS` — canonical alias filename pointing at the active manifest.
- `manifest_filename_for_sha` — deterministic manifest filename for a cmd SHA.
- `aggregate_cmd_sha` — content hash of the aggregation command.
- `write_manifest` — persist a manifest to disk.
- `find_existing_manifests` — list manifests in a result directory.
- `find_manifest_by_cmd_sha` — locate a manifest by its cmd SHA.
- `prune_old_manifests` — keep only the most recent `MAX_MANIFESTS`.
- `build_manifest_with_resume` — manifest builder that reuses prior task IDs
  on resubmit.

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
- `resubmit_plan` — build a `ResubmitPlan` from a manifest + status.

### Combiner

- `run_combiner` — execute the combiner step on the cluster.
- `run_combiner_checked` — same, with explicit error reporting.

### Per-task metrics sidecar

- `write_metrics` — helper executors call to write a sidecar metrics file
  into `$RESULT_DIR`.

### Open boundary question: reduce ownership

The exports under **Reduce**, **Combiner**, and **Per-task metrics sidecar**
above (`reduce_metrics`, `reduce_by_grid_point`, `reduce_partials`,
`reduce_resource_usage`, `run_combiner`, `run_combiner_checked`,
`write_metrics`) are **opt-in convenience currently under review for
extraction to experiment-repo ownership.** They give the cluster-side
data-locality path (per `commands/aggregate.md`) a turnkey default, but the
specific reduction shape (weighted mean with optional `n_samples` weight) is
a framework choice that arguably belongs in the experiment repo. A planned
follow-up will move the reduce *callable* into the experiment repo while
keeping the deployment plumbing (atomic writes, wave-based partial
aggregation, cluster-side execution) in the framework. Until then, treat
these exports as defaults that experiment repos may override by skipping
`write_metrics` and shipping their own reduce step.

## What experiment repos own

Everything outside the framework's public API. Concretely:

- **Executor scripts** under `executors/` or `src/` (any `.py` file with a
  CLI and an `if __name__ == "__main__":` guard — see `discover.py`).
- **Shared utility code** under `lib/` (or wherever the experiment chooses to
  put it).
- **`hpc.yaml`** — optional per-experiment profile config (see
  [`docs/schema.md`](schema.md)).
- **Generated shims** — `date_window_shim.py`, `chunking_shim.py`, or any
  custom shim the LLM writes from the templates in `templates/`. These live
  in the experiment repo, are versioned there, and are user-editable.
- **Domain-specific aggregation** — any `aggregate_cmd` the experiment
  defines for fan-in.

A small set of files are **framework-generated artifacts** that land in the
experiment repo at submit time but are not authored there:
`_hpc_dispatch.json`, `_hpc_dispatch.py`, `_hpc_combiner.py`, and
`hpc_chunking_shim.py`. They are produced by the framework, deployed to the
cluster alongside the experiment code, and overwritten on each `/submit`.

## Reserved filenames

The framework reserves the following basenames inside experiment repos.
Experiment authors must not create files with these names; the discovery
scanner skips them so they are never misclassified as executors. The current
source of truth is `_SKIP_BASENAMES` in
[`hpc_mapreduce/job/discover.py`](../hpc_mapreduce/job/discover.py).

- `_hpc_dispatch.py` — the standalone dispatch script written by the
  framework at submit time.
- `_hpc_combiner.py` — the standalone combiner script written by the
  framework when an aggregation step is configured.
- `hpc_chunking_shim.py` — the framework's row-index chunking shim when the
  user opts into chunked parallelism.

`__init__.py` also appears in `_SKIP_BASENAMES`, but that is a Python package
convention (the discovery scanner skips it because package markers are not
runnable executors), not a framework reservation. Experiment repos may use
`__init__.py` freely.

## Import directions (allowed)

1. **`hpc_mapreduce/**` may import the standard library plus the
   third-party deps listed in `pyproject.toml`** (currently just `pyyaml`).
   No other runtime deps.
2. **`hpc_mapreduce/**` MUST NOT import from `templates/`.** Templates are
   source files copied into experiment repos; treating them as importable
   modules would couple the framework to a fixed set of templates.
3. **`templates/**` MUST NOT import from `hpc_mapreduce/**`** — with one
   narrow exception. Templates ship into experiment repos and run there,
   where `claude-hpc` is generally not installed. The exception: a small
   allowlist of "runtime modules" that `deploy_runtime`
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

- **`config/clusters.yaml`** — cluster infrastructure (host, scheduler,
  scratch path, modules, conda envs, GPU types, throughput constraints).
  Ships with `claude-hpc`. See [`README.md`](../README.md) lines 95–109.
- **`hpc.yaml`** — optional per-experiment profile config (project name,
  grid, resources, results layout). Lives in the experiment repo. See
  [`README.md`](../README.md) lines 111–117 and
  [`docs/schema.md`](schema.md) line 3.

The lint test `test_clusters_yaml_is_infra_only` enforces that
`config/clusters.yaml` only contains infrastructure-shaped keys; any
experiment-shaped field (e.g. `grid`, `executors`) leaking into a cluster
entry will fail it.

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
