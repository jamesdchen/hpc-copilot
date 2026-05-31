---
name: dry-run-local
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.ops.validate.dry_run_local.dry_run_local
---
# dry-run-local

The local **pre-flight execution gate** — the only pre-submit gate that exercises the EXECUTION path before any SSH. Every other gate is static/structural: `check-preflight` probes the env, `validate-executor-signatures` introspects the signature (it calls `resolve(i)` on a sample but never RUNS the executor), `validate-input-dataset` checks the filesystem, the QoS/walltime gates are numeric, `compute_cmd_sha` calls `resolve()` only to hash. The earliest a runtime error (bad import, mis-wired `HPC_KW_*` arg, a broken `result_dir_template`) surfaces today is the cluster-side canary (`verify-canary`) — which runs *after* `rsync_push` + `deploy_runtime` + sbatch/qsub. `dry-run-local` catches the broken-grid class locally, before any cluster cost.

Two layers, deliberately split so the cheap one is default-on:

1. **Template-render check (DEFAULT-ON).** Re-uses the `resolve(i)` sampler `validate-executor-signatures` / `compute_cmd_sha` already walk. For the first `sample_n_tasks` ids it renders `result_dir_template` exactly as the cluster dispatcher's `_format_result_dir` will (`str.format` over `task_id` + `run_id` + kwargs) and flags (a) an **unfilled `{field}`** the kwargs don't supply — a per-task `KeyError` cluster-side — and (b) a **cross-id collision**: two distinct ids that render to the SAME directory, a silent overwrite where wave N clobbers wave M's `metrics.json` and the combiner under-counts.
2. **Executor smoke-exec (OPT-IN, `smoke=true`).** Actually runs the executor for ONE sampled grid point locally, mirroring `models/mapreduce/dispatch.py` semantics (export `HPC_KW_*` + bare uppercase, run the command under a shell with a hard timeout), to catch import / arg-binding bugs. The default command is `executor` verbatim; a `smoke_command` override lets the executor opt into a cheap import / `--help` probe.

Design boundary: a local run can't model the cluster's modules, GPUs, or scale, so the smoke layer is scoped to "catch broken code, not broken cluster" — it COMPLEMENTS `verify-canary`, it never replaces it.

## Inputs

- `result_dir_template` (string, required) — The per-run result-directory template, rendered for the sampled ids.
- `tasks_py_path` (string, default `".hpc/tasks.py"`) — Path to the campaign's tasks.py (relative to experiment_dir).
- `run_id` (string, default `"dry-run-local"`) — Fed into the template render + the smoke run's `HPC_RUN_ID`. A placeholder is fine; the gate never touches the journal.
- `sample_n_tasks` (integer, default 8) — Number of `tasks.resolve(i)` ids to sample for the render / collision check.
- `smoke` (boolean, default false) — Opt in to the executor smoke-exec layer.
- `executor` (string, optional) — The real per-task command; required when `smoke=true`. Must not be the dispatcher command itself (#162).
- `smoke_command` (string, optional) — Override the smoke command with an import/`--help` probe; falls back to `executor`.
- `smoke_task_id` (integer, default 0) — Which sampled id supplies the kwargs/env for the smoke run.
- `smoke_timeout_sec` (integer, default 60) — Hard wall-clock cap on the local smoke run.

## Outputs

A `DryRunLocalResult` object with:

- `findings` (list of `ValidatorFinding` objects) — Empty list = pass. Each finding carries `validator`, `severity`, `code`, `message`, `suggested_fix`, and an `evidence` dict (failing `task_id`; for the smoke layer, the captured `stderr_tail`).

## Errors

None declared on the primitive (no envelope-level `error_code`). Findings carry the diagnostic code instead; common `code` values:

- `tasks_py_missing` (warning) — tasks.py not on disk yet.
- `tasks_py_import_error` (error) — tasks.py raises on import.
- `tasks_py_contract_error` (error) — `total()` / `resolve(i)` raised.
- `resolve_returned_non_dict` (error) — `resolve(i)` returned a non-dict.
- `template_unfilled_field` (error) — `result_dir_template` references a key the kwargs don't supply (a per-task `KeyError` on the cluster).
- `template_render_error` (error) — the template failed to render (bad format spec).
- `result_dir_collision` (error) — two distinct ids render the same dir (silent overwrite of the first task's output).
- `smoke_executor_missing` / `smoke_executor_is_dispatcher` (error) — opt-in misconfig caught before any spawn.
- `smoke_import_error` / `smoke_nonzero_exit` / `smoke_timeout` / `smoke_spawn_error` (error) — the executor failed the local smoke run.

## Idempotency

The template-render layer is pure (reads tasks.py, renders strings). The smoke layer runs the executor locally — idempotent only to the extent the executor's own smoke command is; an import/`--help` probe is.

## Compose with

- Composed by **`validate-campaign`**: it invokes `dry-run-local` whenever `result_dir_template` is supplied (template render default-on; smoke opt-in via `dry_run_smoke`). The `/submit-hpc` cascade (Step 6c) runs `validate-campaign`, so this gate runs there — before the Step 7-8 two-phase canary.
- Predecessor: `build-tasks-py` (materializes tasks.py). Successor on pass: `submit-flow` (Phase 1 canary).

## Notes

- The render mirrors the cluster dispatcher's `_format_result_dir` byte-for-byte (context `{task_id, run_id, **kwargs}`, kwargs win on collision, missing key → `KeyError`), so a template that would die every task on the cluster fails LOCALLY here instead.
- The smoke layer mirrors the dispatcher's kwarg-export contract: each kwarg ships as `HPC_KW_<KEY>` and (unless `HPC_KW_NAMESPACE_ONLY=1`) bare uppercase `<KEY>`, so a `python ...` probe sees the same env the cluster child does.
- Sampling (not an exhaustive walk) keeps the render layer O(`sample_n_tasks`); collisions are caught across the sampled window, where the bug class lives.

**Schemas:** [`dry_run_local.input.json`](../../src/hpc_agent/schemas/dry_run_local.input.json), [`dry_run_local.output.json`](../../src/hpc_agent/schemas/dry_run_local.output.json).
