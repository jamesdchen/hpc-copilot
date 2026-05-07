---
name: validate-campaign
verb: workflow
side_effects: []
idempotent: true
idempotency_key: experiment_dir
error_codes: []
backed_by:
  cli: hpc-mapreduce validate-campaign --spec <path>
  python: claude_hpc.flows.validate_campaign.validate_campaign
---
# validate-campaign

Run every applicable atomic validator (executor signatures, input dataset, walltime vs. history) and aggregate findings into a single `overall` verdict. Each atom is independently skippable: when its required spec field is None, the workflow skips it and notes that in `validators_run` so the agent can distinguish "no findings because nothing checked" from "no findings because everything passed." This is the hook point the submit-flow invokes before any SSH/qsub side effect; an `overall == "fail"` blocks submission.

## Inputs

- `profile` (string) — Profile key (required).
- `cluster` (string) — Cluster key (required).
- `executor_module` (string, optional) — Dotted Python import path of the executor module (e.g., `"src.train"`). When set, `validate-executor-signatures` runs.
- `executor_function` (string, optional) — Function name in executor_module. Required if executor_module is set.
- `dataset_path` (string, optional) — Path to input dataset (parquet/csv/jsonl). When set, `validate-input-dataset` runs.
- `dataset_loader` (string, optional) — One of `"parquet"`, `"csv"`, `"jsonl"`. Required if dataset_path is set.
- `dataset_row_indices` (list of integers, optional) — Row indices that tasks.py references. Required if dataset_path is set.
- `dataset_required_non_null_cols` (list of strings, default `[]`) — Columns that must be non-null.
- `requested_walltime_sec` (integer, optional) — Requested wall-time in seconds. When set, `validate-walltime-against-history` runs.
- `gpu_type` (string, optional) — GPU type for quantile and playbook lookups.
- `workload_tags` (list of strings, default `[]`) — Project-specific tags for known-bad combo checks.

## Outputs

A `ValidateCampaignReport` object with:

- `overall` (string) — One of: `"pass"`, `"warn"`, `"fail"`. Derived from the most-severe finding: any error → fail; else any warning → warn; else pass. Info-level findings never escalate the verdict.
- `findings` (list of `ValidatorFinding` objects) — Aggregated findings from all atoms that ran. Empty list if all validators passed.
- `validators_run` (list of strings) — Names of atomic validators that actually ran (e.g., `["validate-executor-signatures", "validate-input-dataset"]`). Skipped validators are omitted, so the agent knows whether the absence of findings is because nothing was checked.

## Errors

No error codes defined. The workflow returns a report with `overall == "fail"` when findings are present; it does not raise exceptions.

## Idempotency

Idempotency key: `experiment_dir`. The workflow is pure read-only (reads tasks.py, dataset, runtime priors, playbook); calling twice with the same inputs produces the same report.

## Notes

- **Each atom is independently skippable**: When `executor_module` is None, `validate-executor-signatures` is skipped. When `requested_walltime_sec` is None, `validate-walltime-against-history` is skipped. Only atoms with non-None required spec fields run; `validators_run` lists which actually executed.
- **No force flag**: There is no `--force` runtime override. If a rule is wrong for your project, edit `.hpc/playbook.yaml` (version-controlled, per-rule) rather than bypassing the whole validation layer.
- **Agent loop integration**: On `overall == "fail"`, the submit-flow hook aborts and surfaces findings. On `overall == "warn"`, the warnings are printed but submission proceeds. On `overall == "pass"`, submission proceeds silently.
- **Finding codes**: Each finding carries a machine-readable `code` (e.g., `"missing_parameter"`, `"row_index_oob"`, `"walltime_below_quantile"`) and `suggested_fix` hint so the agent loop can apply fixes programmatically without LLM reasoning.
