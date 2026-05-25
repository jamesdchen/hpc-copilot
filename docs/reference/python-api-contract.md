# Python-API Contract

Cross-cutting reference for the Python helpers, on-cluster CLIs, and process-level entry points that slash commands and library callers invoke from inside the `hpc-agent` checkout. Per-operation contracts (input/output/error/idempotency) live in **[`docs/primitives/`](primitives/)** — this file documents only what's shared across operations: the per-run sidecar schema and the conventions the Python surface follows.

> **Looking for the shell `hpc-agent` CLI?** That is the agent-facing surface — see [`docs/reference/cli-spec.md`](cli-spec.md) and the per-subcommand primitives under `docs/primitives/`. This document covers the Python/library and on-cluster paths that `slash_commands/*.py` and the CLI both reach into.

## Conventions

- Stdout is a single JSON object (no logs, no banners). Errors go to stderr.
- Every top-level schema below is stable across `sidecar_schema_version` 1+.
- Structured return shapes use `{<data_key>, errors}` where `errors` is a list of `{code: str, detail: str}` objects (empty list means success).

## Run identity (`.hpc/runs/<run_id>.json`)

Each `submit-spec` invocation writes a per-run sidecar to `.hpc/runs/<run_id>.json`. This is the canonical sidecar schema; downstream primitives (`poll-run-status`, `combine-wave`, `resubmit-failed`, `reconcile-journal`, `campaign-status`'s Python `prior()` form) all read fields from it.

```json
{
  "sidecar_schema_version": 2,
  "run_id": "ml_ridge-20260429-153012-abc12345",
  "cmd_sha": "...",
  "hpc_agent_version": "0.5.0",
  "submitted_at": "2026-04-29T15:30:12Z",
  "executor": "python3 src/ml_ridge.py",
  "result_dir_template": "results/{model}_{seed}",
  "task_count": 24,
  "tasks_py_sha": "...",
  "wave_map": {"0": [0, 1, ...]},

  "cluster": "hoffman2",
  "profile": "ml_ridge",
  "campaign_id": "ml_ridge_optuna_q1",
  "project": "ml-ridge",
  "remote_path": "/u/scratch/u/me/ml_ridge",
  "resources": {"cpus": 4, "mem": "16G", "walltime": "02:00:00"},
  "env": {"modules": "python/3.11.9", "conda_env": "ml"},
  "env_group": "default",
  "constraints": {"max_array_size": 500},
  "gpu_fallback": ["a100", "h100"],
  "max_retries": 3,
  "runtime": "uv",
  "auto_retry": {"oom": {"max_attempts": 2}},
  "aggregate_defaults": {
    "require_outputs": "results/{run_id}/metrics.{task_id}.json",
    "expect_output": "results/{run_id}/metrics.json"
  }
}
```

- `cmd_sha` is computed by `hpc_agent.state.runs.compute_cmd_sha`: `SHA-256(join("\n", json.dumps(tasks.resolve(i), sort_keys=True) for i in range(tasks.total())))`. Stable across equivalent task lists; changes whenever `.hpc/tasks.py` changes the kwargs returned by `resolve`.
- The user's task definition lives in `.hpc/tasks.py` (a Python module exposing `total()` and `resolve(task_id)`); the sidecar references it but does not duplicate per-task data.
- The block from `cluster` through `aggregate_defaults` is the **v2 config snapshot**: every successful `submit-spec` captures the full config it ran under so subsequent primitives read context from the sidecar instead of an external config file. All v2 fields are optional at write time; `read_run_sidecar` backfills missing keys with `None` so callers see a uniform shape regardless of when the sidecar was written.
- `campaign_id` tags the run as part of a closed-loop campaign. The `HPC_CAMPAIGN_ID` env var is forwarded to the cluster by every scheduler template; the user's `tasks.py` reads it back via `os.environ` to call `campaign-status`'s Python form (`hpc_agent.models.mapreduce.reduce.history.prior`) on prior iterations.
- Retention: at most `hpc_agent.state.runs.MAX_RUNS` (default 500; override via `HPC_MAX_RUNS` env var) sidecars are kept per experiment directory. Oldest by mtime are evicted on every write.

When resuming a prior run, the slash command matches the recomputed `cmd_sha` against existing sidecars via `find_run_by_cmd_sha` and delegates to `hpc_agent.ops.recover.batching.resubmit_plan(task_count=, failed_task_ids=)` for the failing task IDs; see `slash_commands/commands/submit-hpc.md` for the interactive resume-vs-fresh prompt.

## Python entry points

The Python surface that slash commands and library callers invoke:

| Operation | Primitive | Python entry point |
|---|---|---|
| Record a submission | [submit-spec](primitives/submit-spec.md) | `hpc_agent.ops.submit.runner.submit_and_record` |
| Poll one run's status | [poll-run-status](primitives/poll-run-status.md) | `hpc_agent.ops.monitor.status.record_status` |
| Combine one wave | [combine-wave](primitives/combine-wave.md) | `hpc_agent.ops.aggregate.combine.combine_wave` |
| Record a resubmission | [resubmit-failed](primitives/resubmit-failed.md) | `hpc_agent.ops.recover.runner.resubmit_failed` |
| Reconcile journal vs cluster | [reconcile-journal](primitives/reconcile-journal.md) | `hpc_agent.ops.monitor.reconcile.reconcile` |
| Mark run terminal | [mark-run-terminal](primitives/mark-run-terminal.md) | `hpc_agent.ops.monitor.reconcile.mark_terminal` |
| Read campaign history | [campaign-status](primitives/campaign-status.md) (Python form) | `hpc_agent.models.mapreduce.reduce.history.prior` |
| List in-flight runs | [list-in-flight](primitives/list-in-flight.md) | `hpc_agent.state.index.find_in_flight_runs` |
| Discover executors | [discover-executors](primitives/discover-executors.md) | `hpc_agent.state.discover.discover_executors` |

The framework also exposes two library-only helpers that are not
primitives (no CLI command, not in `hpc-agent capabilities`) but
remain stable public functions:

| Operation | Python entry point |
|---|---|
| Inspect cluster nodes | `hpc_agent.infra.inspect.inspect_cluster` |
| Roll up runtime priors | `hpc_agent.state.runtime_prior.roll_up_quantiles` |

## Internal cluster-side scripts (not primitives)

The framework also ships three cluster-side Python entry points that downstream primitives invoke over SSH. These are **internal implementation details**, not stable contracts — agents should compose with the primitives above rather than reaching directly into:

- `python -m hpc_agent.models.mapreduce.reduce.status` — backs `poll-run-status`'s remote call. Reads sidecar + queries scheduler, emits per-task JSON.
- `python3 .hpc/_hpc_dispatch.py` — backs the array-job execution. Reads `.hpc/tasks.py`, dispatches one task per `SGE_TASK_ID` / `SLURM_ARRAY_TASK_ID`.
- `python3 .hpc/_hpc_combiner.py` — backs `combine-wave`'s remote call. Aggregates per-task partial reduce JSONs into a wave-level partial.

Implementation lives in `hpc_agent/models/mapreduce/reduce/`, `hpc_agent/runner.py` (the cross-subject re-export bridge), and `hpc_agent/ops/aggregate/combine.py` respectively. Treat the source as the contract; these scripts are not version-pinned across releases.
