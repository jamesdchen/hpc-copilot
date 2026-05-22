---
name: classify-axis
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/.hpc/axes.yaml
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent classify-axis --spec <path>
  python: hpc_agent.atoms.classify_axis.classify_axis
---
# classify-axis

Record a `@register_run` function's classified `DataAxis`
(`Independent` / `Associative` / `BoundedHalo` / `Sequential`) into
`<experiment>/.hpc/axes.yaml`'s `executors` block. The agent does the
*classification* — reading `run()`, conducting the interview (see the
`hpc-classify-axis` skill); this primitive only *records* the resolved
answer. Same agent-reasons / primitive-records split as `axes-init`.

`DataAxis` and the `axes.yaml` *scheduling* axes
(`homogeneous_axes` / `pick_array_axis`) are unrelated concepts:
`DataAxis` is *how to split the totally-ordered series correctly*; the
scheduling axes are *which sweep dimension is promoted onto the task
array*. This primitive touches only the former — the `executors` block —
and round-trips `axes` / `homogeneous_axes` untouched.

## Inputs

- `experiment_dir` (path) — repo root; the file lands at
  `<experiment_dir>/.hpc/axes.yaml`.
- `run_name` (str) — the `@register_run` function's name; the key under
  which the classification is stored in the `executors` block.
- `run_signature_sha` (str) — the run's current signature hash
  (`RunInfo.run_signature_sha` from `discover_runs`). Stored so a later
  submit can detect a signature change and re-interview.
- `data_axis` (object) — the classified series axis:
  `{kind, halo?, monoid?}`. `kind` is one of `independent`,
  `associative`, `bounded_halo`, `sequential`. `bounded_halo` requires a
  `halo: {expr}` block — restricted arithmetic over the run's parameters
  (bare names, numeric literals, `+ - * //`, `min`/`max`); never
  `eval()`'d. `associative` may carry `monoid: sum|moments`.
- `classified_by` (`interview` | `recall` | `manual`, default
  `interview`) — how the classification was reached.

## Outputs

`{axes_path, run_name, data_axis, classified_by, classified_at, wrote}`.
`classified_at` is the ISO-8601 UTC timestamp recorded with the entry.

## Errors

- `spec_invalid` — the `data_axis` block is internally inconsistent: most
  often a `bounded_halo` whose `halo.expr` is not safe arithmetic over
  the run's parameters, or a `kind`/`halo`/`monoid` combination the
  schema rejects.

## Idempotency

Keyed on `experiment_dir`. Re-running with the same spec overwrites
`executors.<run_name>` byte-equivalently modulo the `classified_at`
timestamp. The entry is merged via `upsert_executor`, so other
executors' entries and the scheduling-axis hints survive every call.

## Notes

A classification is a program-analysis judgment and can be wrong — a
misclassified axis runs fine and returns plausible-but-wrong numbers.
`/submit-hpc` runs `hpc_agent.template.assert_elision_equivalent` (whole
run vs split run, assert equality) as a pre-submit gate; recommend the
experiment repo wire it into CI as a required check.
