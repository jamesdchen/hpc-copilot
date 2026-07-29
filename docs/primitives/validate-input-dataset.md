---
name: validate-input-dataset
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.ops.validate.input_dataset.validate_input_dataset
---
# validate-input-dataset

> **Internal primitive** — not surfaced in `capabilities --full`.
> No CLI subcommand and no MCP tool exists to invoke it, so no agent can
> call it directly; `validate-campaign` composes it and its findings reach
> the agent folded into that envelope. `validate-campaign` is the
> agent-facing verb.

Verify that a campaign's input dataset exists, every referenced row is in bounds, and required columns are non-null at those rows. Catches the NaN-trap bug class: a task references row N which exists in the dataset but is NaN at a column the executor reads, so the task survives qsub but crashes at runtime. The validator is generic over parquet (via pyarrow), CSV, and JSONL loaders.

## Composers

- **`validate-campaign`** — the pre-submit validation composer.

## Contract surface

Takes `dataset_path` (parquet, CSV, or JSONL), `loader` (one of `"parquet"` /
`"csv"` / `"jsonl"`), `row_indices` (the row indices `tasks.py` references;
empty list runs a loader-only smoke test), and `required_non_null_cols`
(columns that must be non-null at every referenced row).

Returns a `ValidateInputDatasetResult` whose `findings` list is empty on pass.
Each `ValidatorFinding` carries `validator` / `severity` / `code` / `message` /
`suggested_fix` / `evidence` (row index, column name, `n_rows`).

## Invariants

- **No envelope-level `error_code`.** Diagnostics ride in `findings[].code`:
  `dataset_missing`, `dataset_unsupported_loader`, `dataset_load_error`,
  `parquet_loader_unavailable` (info), `row_index_oob` (valid range
  `[0, n_rows - 1]`), and `required_column_null` — the NaN-trap class.
- **Null means three things.** `None`, `float('nan')`, and the empty string
  `""` all count as null, so the gate catches sentinel placeholders as well as
  genuinely missing values.
- **A missing optional dependency degrades, it does not fail.** When pyarrow is
  absent on a parquet path the validator emits an info-level finding and skips
  validation rather than failing hard, so the campaign can proceed. CSV and
  JSONL use stdlib `csv` / `json` and always work.
- **Proper parsers only.** `wc -l` is never used — it counts newlines, not
  records, and lies on multi-line CSV cells and JSONL with embedded newlines.
- **Pure read.** Same file and spec, same findings.

## Coupling

- `row_indices` must be what `tasks.py` actually references; the composer is
  responsible for deriving them from the same `resolve(i)` walk the other
  gates sample. A divergence makes this gate check rows nobody reads.
- Pyarrow is optional by design; adding a loader means extending both the
  `loader` enum and the null-detection contract above.
- Shares the `ValidatorFinding` shape with the rest of the
  `validate-campaign` family.

## Failure modes

- **Parquet path with no pyarrow** → the NaN-trap class is unguarded for that
  campaign, and the only signal is an info-level finding. Easy to read as a
  pass.
- **Row-level checks skipped silently** when `row_indices` is empty — that is
  the documented loader-only smoke mode, but an empty list arriving by accident
  looks identical to a deliberate one.
- **Large dataset, many columns** → row extraction uses
  `pyarrow.parquet.take()`, which scales; the CSV/JSONL paths do not have the
  same property.

**Schemas:** [`validate_input_dataset.input.json`](../../src/hpc_agent/schemas/validate_input_dataset.input.json), [`validate_input_dataset.output.json`](../../src/hpc_agent/schemas/validate_input_dataset.output.json).
