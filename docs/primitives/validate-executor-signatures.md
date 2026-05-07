---
name: validate-executor-signatures
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce validate-executor-signatures --spec <path>
  python: claude_hpc.atoms.validate_executor_signatures.validate_executor_signatures
---
# validate-executor-signatures

Cross-check a campaign's `tasks.py` against the executor function's signature. Catches the SEGMENT_CHOICES bug class: a campaign's `tasks.resolve(i)` returns kwargs that the executor function would reject at runtime (missing parameter, disallowed Literal value, etc.). The validator samples the first `sample_n_tasks` task indices, inspects the function signature via Python's `inspect` module, and returns actionable findings for every mismatch.

## Inputs

- `executor_module` (string) — Dotted Python import path (e.g., `"myproject.training"`).
- `executor_function` (string) — Function name to introspect in that module.
- `tasks_py_path` (string, default `".hpc/tasks.py"`) — Path to the campaign's tasks.py (relative to experiment_dir).
- `sample_n_tasks` (integer, default 8) — Number of task indices to sample from `tasks.resolve(i)`. Sampling keeps the validator fast for large campaigns without sacrificing the bug-class catch.

## Outputs

A `ValidateExecutorSignaturesResult` object with:

- `findings` (list of `ValidatorFinding` objects) — Empty list = pass. Each finding has:
  - `validator` — `"validate-executor-signatures"`
  - `severity` — `"error"`, `"warning"`, or `"info"`
  - `code` — Machine-readable error code (see below).
  - `message` — Human-readable description.
  - `suggested_fix` — Actionable hint (e.g., "Add parameter `mode` to the executor function").
  - `evidence` — Raw values (task index, parameter name, allowed values, etc.).

## Errors

None declared on the primitive (no envelope-level `error_code`). Findings carry the diagnostic code instead; common `code` values:

- `tasks_py_missing` (warning) — campaign hasn't been interviewed yet; tasks.py doesn't exist.
- `tasks_py_import_error` (error) — tasks.py exists but raises on import.
- `executor_module_import_error` (info) — module path wrong or module fails to import; signature check skipped, validator-level pass.
- `executor_function_not_found` (error) — function name typo or attribute is not callable.
- `missing_parameter` (error) — `tasks.resolve(i)` passes a kwarg the function has no parameter for (and no `**kwargs`).
- `literal_value_not_allowed` (error) — parameter annotated `Literal[...]` but the kwarg value isn't in the allowed set (the SEGMENT_CHOICES bug class).
- `resolve_returned_non_dict` (error) — `tasks.resolve(i)` returned a non-dict; the framework requires a dict so kwargs can be `**`-unpacked.

## Idempotency

The validator reads tasks.py and the executor module; calling twice with the same code produces the same findings.

## Notes

- When the executor module fails to import (e.g., missing optional dependency, import-time side effect), the validator emits an info-level finding and skips the signature check rather than failing hard. This lets the rest of the campaign validate.
- The validator uses `inspect.signature()` so it supports type annotations like `Literal`, `Union`, `Optional`, and standard type hints. If a parameter has no annotation, any value passes the check.
- Sampling instead of exhaustive walk keeps the validator O(sample_n_tasks) rather than O(tasks); the first failing task surfaces a finding, so larger samples reduce statistical noise without linear cost scaling.

**Schemas:** [`validate_executor_signatures.input.json`](../../src/claude_hpc/schemas/validate_executor_signatures.input.json), [`validate_executor_signatures.output.json`](../../src/claude_hpc/schemas/validate_executor_signatures.output.json).
