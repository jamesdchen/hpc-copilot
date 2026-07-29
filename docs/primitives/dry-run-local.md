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

> **Internal primitive** — not surfaced in `capabilities --full`.
> No CLI subcommand and no MCP tool exists to invoke it, so no agent can
> call it directly; `validate-campaign` and `submit-flow` compose it and its
> findings reach the agent folded into their envelopes. Those are the
> agent-facing verbs.

The local **pre-flight execution gate** — the only pre-submit gate that exercises the EXECUTION path before any SSH. Every other gate is static/structural: `check-preflight` probes the env, `validate-executor-signatures` introspects the signature (it calls `resolve(i)` on a sample but never RUNS the executor), `validate-input-dataset` checks the filesystem, the QoS/walltime gates are numeric, `compute_cmd_sha` calls `resolve()` only to hash. The earliest a runtime error (bad import, mis-wired `HPC_KW_*` arg, a broken `result_dir_template`) surfaces today is the cluster-side canary (`verify-canary`) — which runs *after* `rsync_push` + `deploy_runtime` + sbatch/qsub. `dry-run-local` catches the broken-grid class locally, before any cluster cost.

Two layers, deliberately split so the cheap one is default-on:

1. **Template-render check (DEFAULT-ON).** Re-uses the `resolve(i)` sampler `validate-executor-signatures` / `compute_cmd_sha` already walk. For the first `sample_n_tasks` ids it renders `result_dir_template` exactly as the cluster dispatcher's `_format_result_dir` will (`str.format` over `task_id` + `run_id` + kwargs) and flags (a) an **unfilled `{field}`** the kwargs don't supply — a per-task `KeyError` cluster-side — and (b) a **cross-id collision**: two distinct ids that render to the SAME directory, a silent overwrite where wave N clobbers wave M's `metrics.json` and the combiner under-counts.
2. **Executor smoke-exec (OPT-IN, `smoke=true`).** Actually runs the executor for ONE sampled grid point locally, mirroring `execution/mapreduce/dispatch.py` semantics (export `HPC_KW_*` + bare uppercase, run the command under a shell with a hard timeout), to catch import / arg-binding bugs. The default command is `executor` verbatim; a `smoke_command` override lets the executor opt into a cheap import / `--help` probe.

Design boundary: a local run can't model the cluster's modules, GPUs, or scale, so the smoke layer is scoped to "catch broken code, not broken cluster" — it COMPLEMENTS `verify-canary`, it never replaces it.

## Composers

- **`validate-campaign`** — invokes `dry-run-local` whenever `result_dir_template` is supplied (template render default-on; smoke opt-in via `dry_run_smoke`). The `/submit-hpc` cascade (Step 6c) runs `validate-campaign`, so this gate runs there — before the Step 7-8 two-phase canary.
- **`submit-flow`** — the same gate on the submit path.

Predecessor: `build-tasks-py` (materializes tasks.py). Successor on pass: `submit-flow` (Phase 1 canary).

## Contract surface

Driven by a `DryRunLocalSpec`. The load-bearing knobs are `result_dir_template`
(required — the template rendered for the sampled ids), `tasks_py_path`
(default `".hpc/tasks.py"`), `sample_n_tasks` (default 8), and the opt-in smoke
block: `smoke` (default false), `executor` (required when `smoke=true`; must not
be the dispatcher command itself, #162), `smoke_command`, `smoke_task_id`,
`smoke_timeout_sec` (default 60). `run_id` defaults to the placeholder
`"dry-run-local"` — the gate never touches the journal.

Returns a `DryRunLocalResult` whose `findings` list is empty on pass. Each
`ValidatorFinding` carries `validator` / `severity` / `code` / `message` /
`suggested_fix` / `evidence` (the failing `task_id`; for the smoke layer, the
captured `stderr_tail`).

## Invariants

- **No envelope-level `error_code`.** Diagnostics ride in `findings[].code`, never
  on the error channel: `tasks_py_missing` (warning), `tasks_py_import_error`,
  `tasks_py_contract_error`, `resolve_returned_non_dict`,
  `template_unfilled_field`, `template_render_error`, `result_dir_collision`,
  `smoke_executor_missing`, `smoke_executor_is_dispatcher`, `smoke_import_error`,
  `smoke_nonzero_exit`, `smoke_timeout`, `smoke_spawn_error`.
- **The render mirrors the cluster byte-for-byte.** Context is
  `{task_id, run_id, **kwargs}`, kwargs win on collision, a missing key raises
  `KeyError` — the same contract as the dispatcher's `_format_result_dir`, so a
  template that would die every task on the cluster fails LOCALLY here instead.
- **The smoke layer mirrors the dispatcher's kwarg-export contract.** Each kwarg
  ships as `HPC_KW_<KEY>` and (unless `HPC_KW_NAMESPACE_ONLY=1`) bare uppercase
  `<KEY>`, so a `python ...` probe sees the same env the cluster child does.
- **Sampled, not exhaustive.** The render layer is O(`sample_n_tasks`);
  collisions are caught across the sampled window, where the bug class lives.
- **Layer 1 is pure.** Template rendering only reads tasks.py and formats
  strings. Layer 2 is repeatable only to the extent the executor's own smoke
  command is — an import/`--help` probe is.

## Coupling

- The render must track `execution/mapreduce/dispatch.py::_format_result_dir`. A
  change to how the cluster builds a result dir that does not land here
  re-opens the collision/`KeyError` class this gate exists to close.
- The smoke layer must track the same module's env-export contract
  (`HPC_KW_*`, `HPC_KW_NAMESPACE_ONLY`).
- Shares the `resolve(i)` sampler with `validate-executor-signatures` and
  `compute_cmd_sha`; `sample_n_tasks` semantics should stay aligned across them.

## Failure modes

- **Executor module has an import-time side effect** → the smoke layer runs it
  locally. Scoped by `smoke_timeout_sec`, but a heavyweight executor is a reason
  to supply `smoke_command` with a cheap `--help` probe rather than the real
  command.
- **A collision outside the sampled window** → not caught. Raising
  `sample_n_tasks` widens the window; exhaustive checking is deliberately not
  offered (it is O(tasks)).
- **`smoke=true` with `executor` set to the dispatcher command** → caught as
  `smoke_executor_is_dispatcher` before any spawn, not at runtime.

**Schemas:** [`dry_run_local.input.json`](../../src/hpc_agent/schemas/dry_run_local.input.json), [`dry_run_local.output.json`](../../src/hpc_agent/schemas/dry_run_local.output.json).
