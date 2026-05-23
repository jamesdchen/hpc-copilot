# Boundary Contract: hpc-agent ↔ experiment repos

## Purpose

This document is the single source of truth for the boundary between the
`hpc-agent` framework and the experiment repos it orchestrates. It enumerates
(a) the public Python API the framework offers, (b) the filenames the framework
reserves inside experiment repos, (c) the allowed import directions, and (d)
the split between cluster-infrastructure config and per-experiment config. The
allowlist-style lint test in
[`tests/test_boundary_contract.py`](../tests/test_boundary_contract.py)
enforces these lists; any drift fails CI with an actionable diff pointing back
here.

## What hpc-agent owns (public API allowlist)

The exports below are the entire public surface of the `hpc_agent`
package. Groupings mirror those in
[`hpc_agent/__init__.py`](../src/hpc_agent/__init__.py).

The public boundary also now includes the **shell CLI** at
`hpc_agent/agent_cli.py` (entry point `hpc-agent`). Its envelope
shape, subcommand list, and exit-code contract are documented in
[`docs/reference/cli-spec.md`](cli-spec.md) and the JSON Schemas under
`hpc_agent/schemas/`. The JSON Schemas are themselves a build
artifact — they're regenerated from Pydantic models under
`src/hpc_agent/_schema_models/` by `scripts/build_schemas.py`.
External consumers read the JSON files (that's the wire contract);
internal contributors edit the Pydantic. The CLI calls into the same
atomic-ops layer (`hpc_agent/runner/`) that the slash commands use,
so the invariants in [`docs/internals/sync-checklist.md`](../internals/sync-checklist.md)
bind both.

### Package root

- `_PACKAGE_ROOT` — absolute path to the **package directory**
  (`hpc_agent/`, where `__init__.py` lives). Used to resolve
  `hpc_agent/config/clusters.yaml`, the bundled job templates
  under `hpc_agent/mapreduce/templates/runtime/<scheduler>/`, and
  starter scaffolds under `hpc_agent/mapreduce/templates/scaffolds/`.
  Note: this points at the package, **not** the repo root.
- `__version__` — package version string, resolved at import time from
  the installed distribution metadata. Falls back to
  `"0.0.0+unknown"` when running from a non-installed checkout.

### Config & discovery

- `load_clusters_config` — parse and return `hpc_agent/config/clusters.yaml`.
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
- `RepoLayout` — frozen dataclass; canonical home for the three
  forwarders above. New code prefers
  `RepoLayout(experiment_dir).hpc | .runs | .tasks |
  .run_sidecar(run_id) | .runtime_prior(profile, cluster)`.
- `JournalLayout` — frozen dataclass; the cross-experiment journal
  tree under `~/.claude/hpc/<repo_hash>/`. Distinct *type* from
  `RepoLayout` so the pre-B1 `runs_dir` (journal) vs `runs_subdir`
  (cluster sidecar) name collision is now a type error.

### Per-run sidecars

A sidecar `.hpc/runs/<run_id>.json` carries per-run state for one
submission. Identity fields: `run_id`, `cmd_sha`, `hpc_agent_version`,
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

- `hpc_agent.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` —
  read-only per-iteration reduced metrics for a campaign, oldest-first.
  Pure local filesystem walk. Does not import `.hpc/tasks.py`.
- `hpc_agent.mapreduce.reduce.history.find_sidecars_by_campaign` /
  `result_dirs_for_sidecar` — underlying primitives.
- `hpc_agent.campaign.campaign_dir(experiment_dir, campaign_id)` —
  return `experiment_dir/.hpc/campaigns/<campaign_id>/`, creating it
  idempotently. Reserved for strategy libraries to drop their own state
  files (Optuna SQLite, PBT checkpoints). The framework writes nothing
  inside. There is no Python driver; the loop is repeated `/submit-hpc
  campaign_id=<slug>` invocations from the slash-command surface.
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

### Smart-submit data layer

Resource-quality-aware data helpers. State lives under the
experiment's `.hpc/`: `runtimes/<profile>.<cluster>.json` (runtime
samples).

- `inspect_cluster` — read-only per-node snapshot of a cluster
  (alloc-mem%, CPU load, GRES, co-tenants, drain). 60s in-process cache.
- `append_runtime_sample` — append one task's elapsed-time + node +
  gpu_type to the runtime priors log; idempotent on `(run_id, task_id)`.
- `roll_up_runtime_quantiles` — group samples by `gpu_type`, return
  p50/p95/p99/mean/n_samples plus a `needs_canary` flag.

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
  `hpc_agent/mapreduce/templates/scaffolds/tasks_example.py`), git-tracked, and
  user-editable. The bridge between the framework's task-id contract
  and whatever parallelization axis the experiment needs.
- **`.hpc/stages.py`** (optional) — the user-written Python module
  exposing `stages() -> list[dict]` for multi-stage DAG submissions.
  Validated against `hpc_agent/schemas/stages.input.json` at load
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
[`hpc_agent/state/discover.py`](../src/hpc_agent/state/discover.py), so
nothing inside it is misclassified as an executor. Experiment authors
must not place user-code files (executors, libraries) under `.hpc/`.

`_SKIP_BASENAMES` retains `__init__.py` so package markers are not
treated as runnable executors. That is a Python package convention, not
a framework reservation; experiment repos may use `__init__.py` freely.

## Import directions (allowed)

1. **`hpc_agent/**` may import the standard library plus the
   third-party deps listed in `pyproject.toml`** (currently just `pyyaml`).
   No other runtime deps.
2. **`hpc_agent/**` MUST NOT import from
   `hpc_agent/mapreduce/templates/`.** Templates are source files
   copied into experiment repos; treating them as importable modules
   would couple the framework to a fixed set of templates.
3. **`hpc_agent/mapreduce/templates/**` MUST NOT import from
   `hpc_agent/**`** — with narrow exceptions. Templates ship
   into experiment repos and run there, where `hpc-agent` is
   generally not installed. The exception: a small allowlist of
   "runtime modules" that `deploy_runtime`
   (`hpc_agent/infra/remote.py`) explicitly copies onto the cluster
   compute node alongside the executor. Templates may import from those
   because they are guaranteed to be present at execution time. The current
   allowlist (kept in sync with `RUNTIME_MODULES_ALLOWED_IN_TEMPLATES` in
   `tests/contracts/test_boundary_contract.py`) is:
   - `hpc_agent.mapreduce.metrics_io` — the `write_metrics` sidecar
     writer plus the `read_kw_env` kwargs-from-env helper. Stdlib-only,
     deployed alongside `combiner.py`. Templates use a lazy import
     gated on `$RESULT_DIR` so smoke tests still run without
     `hpc-agent` installed.
   - `hpc_agent.executor_cli` — the `flag`, `generic_args`,
     `gpu_args`, and `build_parser_from_flags` helpers used by the
     canonical `tasks.py` template (see
     `hpc_agent/mapreduce/templates/scaffolds/tasks_example.py`) and
     the auto-generated `.hpc/cli.py` dispatcher. Stdlib-only,
     deployed alongside `metrics_io.py` by `deploy_runtime`.

   To add a new entry, the module must (a) be deployed by `deploy_runtime`,
   (b) be stdlib-only or self-contained, and (c) be added to both the
   allowlist constant in the lint test and this doc in the same PR.
4. **`tests/**` may import either** — tests live in the framework repo and
   exercise both sides.

## Config split

Cluster-infrastructure config and per-experiment config are deliberately
separated, so that adding a new experiment never requires editing the
framework, and adding a new cluster never requires touching any experiment.

- **`hpc_agent/config/clusters.yaml`** — cluster infrastructure
  (host, scheduler, scratch path, modules, conda envs, GPU types,
  throughput constraints). Ships with `hpc-agent`. See
  [`README.md`](../README.md).
- **Per-run sidecars at `.hpc/runs/<run_id>.json`** — the v2 schema
  captures the full per-experiment config snapshot (resources, env,
  constraints, profile name, runtime, auto_retry, aggregate defaults)
  for each successful submit. Subsequent commands read it instead of a
  separate experiment-config file. Conversational `/submit` writes one;
  there is no user-authored experiment-config yaml.

The lint test `test_clusters_yaml_is_infra_only` enforces that
`hpc_agent/config/clusters.yaml` only contains infrastructure-shaped
keys; any experiment-shaped field (e.g. `grid`, `executors`) leaking
into a cluster entry will fail it.

## Determinism contract

The framework's value proposition is "parallelize an experiment without
changing what it computes." This section enumerates what the framework
guarantees about parity between a serial run of the user's executor
and the same task running as part of a parallel array, what guardrails
are wired in by default, and where determinism is fundamentally on the
user.

### What the framework guarantees

- **Per-task isolation.** Each task runs in a fresh subprocess
  (`mapreduce/dispatch.py:Popen`); no in-process state leaks across
  tasks. The task's `RESULT_DIR` is a per-task `_wip_<task_id>/`
  tempdir that atomically promotes to the final dir on exit-0.
- **Deterministic env defaults.** The cluster preamble
  (`hpc_agent/mapreduce/templates/runtime/common/hpc_preamble.sh`) exports
  `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`,
  `NUMEXPR_NUM_THREADS=1`, `VECLIB_MAXIMUM_THREADS=1`,
  `PYTHONUNBUFFERED=1`, `PYTHONHASHSEED=0`,
  `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8`,
  `LC_ALL=C.UTF-8`, `LANG=C.UTF-8`. The GPU preamble adds
  `CUBLAS_WORKSPACE_CONFIG=:4096:8` and
  `XLA_FLAGS=--xla_gpu_deterministic_ops=true`. Each is overridable via
  the matching `HPC_<NAME>` env var in the spec's `job_env`; the
  empty string disables the export entirely.
- **Order-invariant aggregation.** The combiner
  (`mapreduce/combiner.py`) uses `sorted()` for grid-key and
  per-key iteration plus Neumaier-compensated summation. Re-running
  the combiner on the same per-task outputs produces bit-identical
  aggregates regardless of which task finished first.
- **Identity-tracked re-runs.** Each successful task stamps
  `<result_dir>/.hpc_cmd_sha`. On re-entry, the dispatcher compares
  this against the current sidecar's `cmd_sha` and re-runs the
  executor when they differ — so a code/kwarg change never silently
  reuses a stale `metrics.json`. `HPC_FORCE_RERUN=1` bypasses the
  idempotency skip unconditionally.
- **Collision-free kwarg namespace.** The dispatcher exports each
  kwarg as `HPC_KW_<KEY>=<value>`. The legacy bare-uppercase form
  `<KEY>=<value>` is exported by default for back-compat; setting
  `HPC_KW_NAMESPACE_ONLY=1` disables it. `build-tasks-py` rejects
  axis names whose uppercase form would shadow a real env var
  (`HOME`, `PATH`, `LD_LIBRARY_PATH`, `OMP_NUM_THREADS`, framework-
  reserved `HPC_*`, scheduler-injected `SLURM_*`/`SGE_*`/`PBS_*`,
  ...) — see `_RESERVED_AXIS_NAMES` and `_RESERVED_AXIS_PREFIXES`
  in `incorporation/build/tasks_py.py`.

### What the framework does not — and cannot — guarantee

- **Floating-point identity across GPU SKUs.** Different GPU models run
  different CUDA kernels with different reduction trees. To pin: set
  a single entry in `clusters.yaml`'s `gpu_types`, or pass a single-
  element `gpu_fallback`.
- **Identical outputs across Python or library versions.** Pin
  `modules: [python/3.11.9]` in `clusters.yaml` and use a locked
  conda env. If `HPC_RUNTIME=uv`, commit `uv.lock`.
- **BLAS backend identity.** Conda envs that pull whichever BLAS is
  available (OpenBLAS vs. MKL vs. Apple Accelerate) produce subtly
  different float results. Pin the BLAS provider in your env.
- **Determinism inside the executor.** RNG seeding, library-level
  determinism flags (`torch.use_deterministic_algorithms(True)`,
  `np.random.default_rng(seed)` over `np.random.seed`), and avoiding
  wallclock-driven branches are the user's responsibility. The
  scaffold at `templates/scaffolds/executor_template.py` demonstrates
  the recommended seed-from-`HPC_TASK_ID` pattern.

### Reproducing one task locally

To reproduce a task's behavior outside the dispatcher (e.g. for a
local reference run), set the env vars the dispatcher would set:

```sh
HPC_TASK_ID=0 \
HPC_RUN_ID=local-ref \
RESULT_DIR=/tmp/ref \
HPC_KW_HORIZON=5 HPC_KW_SEED=42 \
python executors/ml_ridge.py --horizon 5 --seed 42 --output-file /tmp/ref/out.csv
```

A task that produces bit-identical output here and on the cluster has
no framework-induced divergence; any gap traces back to one of the
"cannot guarantee" items above (GPU/Python/lib version drift).

## How to extend

When adding a new public export, a new reserved filename, or a new template,
update **this document and `tests/test_boundary_contract.py`** in the same
PR as the code change. The lint test compares its allowlist constants
against the live module attributes; any unannounced addition or removal will
fail with a diff that points back here.

Specifically:

- New public export → add it to `__all__` in
  `hpc_agent/__init__.py`, list it in the appropriate section above,
  and add it to `ALLOWED_EXPORTS` in the test.
- New reserved filename → add it to `_SKIP_BASENAMES` in
  `hpc_agent/state/discover.py`, list it under "Reserved filenames"
  above, and add it to `RESERVED_FILES` in the test.
- New cluster-config key → add it to `ALLOWED_CLUSTER_KEYS` in the test
  and document it under "Config split" (and in `docs/schema.md` if
  user-facing).
