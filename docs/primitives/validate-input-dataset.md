---
name: validate-input-dataset
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: claude_hpc.atoms.validate_input_dataset.validate_input_dataset
---
# validate-input-dataset

Verify that a campaign's input dataset exists, every referenced row is in bounds, and required columns are non-null at those rows. Catches the NaN-trap bug class: a task references row N which exists in the dataset but is NaN at a column the executor reads, so the task survives qsub but crashes at runtime. The validator is generic over parquet (via pyarrow), CSV, and JSONL loaders.

## Inputs

- `dataset_path` (string) — Path to the input dataset file (parquet, CSV, or JSONL).
- `loader` (string) — One of: `"parquet"`, `"csv"`, `"jsonl"`.
- `row_indices` (list of integers, min length 1) — Row indices that `tasks.py` references.
- `required_non_null_cols` (list of strings, default `[]`) — Columns that must be non-null at every referenced row.

## Outputs

A `ValidateInputDatasetResult` object with:

- `findings` (list of `ValidatorFinding` objects) — Empty list = pass. Each finding includes:
  - `validator` — `"validate-input-dataset"`
  - `severity` — `"error"`, `"warning"`, or `"info"`
  - `code` — Machine-readable error code.
  - `message` — Human-readable description.
  - `suggested_fix` — Actionable hint.
  - `evidence` — Raw values (row index, column name, n_rows, etc.).

## Errors

None declared on the primitive. Findings carry the diagnostic code instead; common `code` values:

- `dataset_missing` (error) — file not on disk.
- `dataset_unsupported_loader` (error) — loader name not in {parquet, csv, jsonl}.
- `dataset_load_error` (error) — loader raised an exception (parse error, corrupt file, etc.).
- `parquet_loader_unavailable` (info) — pyarrow optional dependency missing; parquet validation skipped, campaign can proceed.
- `row_index_oob` (error) — index out of bounds (valid range: `[0, n_rows - 1]`).
- `required_column_null` (error) — the NaN-trap bug class; a required column is null / NaN / empty-string at the row.

## Idempotency

The validator reads the dataset file; calling twice with the same file and spec produces the same findings.

## Notes

- The validator treats `None`, `float('nan')`, and empty strings (`""`) as null. This catches both missing values and sentinel placeholders.
- Pyarrow is an optional dependency; when absent for a parquet path, the validator emits an info-level finding and skips validation rather than failing hard. CSV and JSONL use stdlib modules (csv, json) and always work.
- The validator uses `pyarrow.parquet.take()` for efficient row extraction, so it scales well even for large datasets with many columns.
- `wc -l` is never used — it counts newlines, not records, and lies on multi-line CSV cells or JSONL with embedded newlines. The validator uses proper parsers.

**Schemas:** [`validate_input_dataset.input.json`](../../src/claude_hpc/schemas/validate_input_dataset.input.json), [`validate_input_dataset.output.json`](../../src/claude_hpc/schemas/validate_input_dataset.output.json).
