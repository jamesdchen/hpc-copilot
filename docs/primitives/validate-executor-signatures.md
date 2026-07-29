---
name: validate-executor-signatures
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.ops.validate.executor_signatures.validate_executor_signatures
---
# validate-executor-signatures

> **Internal primitive** — not surfaced in `capabilities --full`.
> No CLI subcommand and no MCP tool exists to invoke it, so no agent can
> call it directly; `validate-campaign` composes it and its findings reach
> the agent folded into that envelope. `validate-campaign` is the
> agent-facing verb.

Cross-check a campaign's `tasks.py` against the executor function's signature. Catches the SEGMENT_CHOICES bug class: a campaign's `tasks.resolve(i)` returns kwargs that the executor function would reject at runtime (missing parameter, disallowed Literal value, etc.). The validator samples the first `sample_n_tasks` task indices, inspects the function signature via Python's `inspect` module, and returns actionable findings for every mismatch.

## Composers

- **`validate-campaign`** — the pre-submit validation composer; runs this gate
  as part of its cascade.
- `infra/executor_import.py` shares the module-import path this validator uses.

## Contract surface

Driven by a `ValidateExecutorSignaturesSpec`: `executor_module` (dotted import
path, e.g. `"myproject.training"`), `executor_function` (the name introspected
in that module), `tasks_py_path` (default `".hpc/tasks.py"`), and
`sample_n_tasks` (default 8 — sampling keeps the validator fast for large
campaigns without sacrificing the bug-class catch).

Returns a `ValidateExecutorSignaturesResult` whose `findings` list is empty on
pass. Each `ValidatorFinding` carries `validator` / `severity` / `code` /
`message` / `suggested_fix` (e.g. "Add parameter `mode` to the executor
function") / `evidence` (task index, parameter name, allowed values).

## Invariants

- **No envelope-level `error_code`.** Diagnostics ride in `findings[].code`:
  `tasks_py_missing` (warning — campaign not interviewed yet),
  `tasks_py_import_error`, `executor_module_import_error` (info),
  `executor_function_not_found`, `missing_parameter`,
  `literal_value_not_allowed` (the SEGMENT_CHOICES class), and
  `resolve_returned_non_dict`.
- **A broken executor import degrades, it does not fail.** When the executor
  module fails to import (missing optional dependency, import-time side
  effect), the validator emits an info-level finding and SKIPS the signature
  check rather than failing hard, so the rest of the campaign still validates.
- **Annotation-driven, absence-tolerant.** `inspect.signature()` supports
  `Literal`, `Union`, `Optional`, and standard hints; an unannotated parameter
  accepts any value.
- **Sampled, not exhaustive.** O(`sample_n_tasks`) rather than O(tasks). The
  first failing task surfaces a finding, so a larger sample reduces noise
  without linear cost scaling.
- **Introspects, never executes.** It calls `resolve(i)` on the sample to get
  kwargs but never RUNS the executor — that is `dry-run-local`'s job.

## Coupling

- Shares the `resolve(i)` sampler and the `sample_n_tasks` knob with
  `dry-run-local` and `compute_cmd_sha`; the three should stay aligned.
- The `tasks.resolve(i)` → dict contract is load-bearing: the framework
  `**`-unpacks the result, so a non-dict is a finding, not a tolerance.
- Sits directly upstream of `dry-run-local` in the gate ladder — static
  signature check first, execution check second.

## Failure modes

- **Executor module imports but the function is re-exported from elsewhere** →
  `inspect.signature()` resolves the underlying callable, so a decorator that
  does not `functools.wraps` can surface a spurious `missing_parameter`.
- **`**kwargs` in the executor signature** → suppresses `missing_parameter`
  entirely; the gate cannot see through a catch-all, and the mismatch resurfaces
  at runtime on the cluster.
- **Sampled window misses a rare grid point** → a `literal_value_not_allowed`
  that only occurs at a high task index is not caught. Raise `sample_n_tasks`.

**Schemas:** [`validate_executor_signatures.input.json`](../../src/hpc_agent/schemas/validate_executor_signatures.input.json), [`validate_executor_signatures.output.json`](../../src/hpc_agent/schemas/validate_executor_signatures.output.json).
