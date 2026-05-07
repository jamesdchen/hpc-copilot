# /validate-campaign — Pre-submit static validation

Cross-checks a campaign before any SSH or qsub. Catches three bug classes
that otherwise surface hours later in the queue:

1. **Fabricated kwargs** — `tasks.py.resolve(i)` passes values the
   executor function's signature would reject (e.g. `Literal["a", "b", "c"]`
   parameter receiving `"x"`).
2. **NaN-trap row references** — `tasks.py` indices into a parquet/csv/jsonl
   that exists at the index but is null at columns the executor reads.
3. **Walltime / GPU mismatches against history** — requested walltime is
   below the historical p95, or `(gpu_type, workload_tag)` matches a
   project-recorded known-bad combination in `.hpc/playbook.yaml` (e.g.
   V100 + attn-fp32 unstable).

`/submit-hpc` and `/campaign-hpc` invoke this primitive automatically
before any side-effecting step. Run `/validate-campaign` directly when
you want to dry-run validation without submitting (e.g. while iterating
on `tasks.py`).

## Steps

1. Build the spec. The minimum-viable shape:

   ```json
   {
     "profile": "ml_ridge",
     "cluster": "discovery",
     "executor_module": "src.train",
     "executor_function": "main",
     "dataset_path": "data/inputs.parquet",
     "dataset_loader": "parquet",
     "dataset_row_indices": [0, 1, 2, 5, 8],
     "dataset_required_non_null_cols": ["target"],
     "requested_walltime_sec": 7200,
     "gpu_type": "a100",
     "workload_tags": ["attn-fp32"]
   }
   ```

   Every block is optional. Omit `executor_module` to skip the
   signature check; omit `dataset_path` to skip the dataset check;
   omit `requested_walltime_sec` to skip the walltime/playbook check.

2. Invoke the CLI:

   ```bash
   python -m claude_hpc validate-campaign --spec spec.json --experiment-dir .
   ```

   Output is a single-line JSON envelope on stdout. Parse it.

3. For each `data.findings` entry:

   - `severity == "error"` — submission must NOT proceed; apply
     `suggested_fix` (if present), edit the relevant input, re-run
     validation.
   - `severity == "warning"` — informational; surface to the operator
     and proceed if they accept the trade-off.
   - `severity == "info"` — purely advisory (e.g. cold-start, no
     samples yet); no action needed.

4. Common `code` values + recommended response:

   - `literal_value_not_allowed` → fix the offending value in
     `tasks.py.resolve(i)` per `evidence.allowed`.
   - `missing_parameter` → either remove the kwarg from `tasks.py`
     or add it to the executor function signature.
   - `row_index_oob` → drop the index from `tasks.py` or extend
     the dataset.
   - `required_column_null` → drop the row from `tasks.py` or
     backfill the column.
   - `walltime_below_quantile` → raise `requested_walltime_sec` to
     `evidence.quantile_sec`.
   - `known_bad_combination` → switch GPU type or remove the workload
     tag for this campaign.

5. If `data.overall == "fail"`, do NOT advance to `/submit-hpc`. Apply
   fixes, re-run `/validate-campaign`, and proceed only on
   `pass` or `warn`.

## Notes

- Idempotent. Safe to call as many times as you want; no side effects.
- No `--force` flag exists by design. If a rule is wrong for your
  project, edit `.hpc/playbook.yaml` to disable or relax it (one
  version-controlled commit) rather than override at runtime.
- `.hpc/playbook.yaml` schema (every section optional):

  ```yaml
  known_bad_combinations:
    - gpu: v100
      workload_tag: attn-fp32
      severity: error
      reason: "V100 fp32 attention is numerically unstable"

  walltime_rules:
    - below_quantile: 0.95
      severity: warning
      message: "Requested walltime is below historical p95"
  ```
