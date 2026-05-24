---
name: verify-aggregation-complete
verb: query
side_effects: []
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent verify-aggregation-complete [--experiment-dir <dir>] --run-id <run_id>
    --combiner-dir <combiner_dir_local> [--results-dir <results_dir_local>]
  python: hpc_agent.ops.aggregate.invariants.verify_aggregation_complete
exit_codes:
- 0: ok
- 1: user-error
---
# verify-aggregation-complete

> **Internal primitive.** Invariant check composed transitively
> by `aggregate-flow`'s post-pull validation step.

Walk the run sidecar's `wave_map` + the locally-pulled
`_combiner/` directory and report the framework-knowable
post-aggregate invariants. The user's aggregation OUTPUT is
opaque (framework doesn't know what `qlike_score=0.42` means),
but the INVARIANTS — every wave combined, every task accounted
for, provenance present, no cross-run contamination — ARE
framework-knowable.

## Composers

- `aggregate-flow` (post-pull invariant gate). Runs after the
  rsync_pull pulls `_combiner/` locally, before the workflow
  emits the result envelope.
- Operator debugging when an aggregation result smells wrong
  (`hpc-agent verify-aggregation-complete --run-id <id>
  --combiner-dir <path>`).

## Invariants

- **Pure read-only.** Local files only; no SSH, no journal
  mutation.
- **Six independent checks.** `all_waves_combined`,
  `all_tasks_present`, `provenance_present`, plus the count
  comparisons. `ok` is True iff every individual check passes.
- **`unexpected_tasks` is the cross-run-contamination
  signal.** Task IDs in the partials but NOT in the run's
  `wave_map` mean a partial was rsync'd from a different run.
  Red flag the agent should escalate.

## Coupling

- The `wave_map` schema lives on the per-run sidecar
  (`state/runs.py:write_run_sidecar`). Renaming or restructuring
  `wave_map` cascades through this atom.
- The `_combiner/wave_<N>.json` filename convention is the
  combiner's contract. A combiner refactor that changes the
  filename pattern breaks this primitive's pulled-wave detection.
- Runtime sidecars (`wave_*.runtime.json`) are deliberately
  skipped — they're the warm-picker pipeline's per-wave runtime
  records, not aggregation partials. Adding a new sibling
  filename pattern means updating this atom's exclusion list.

## Failure modes

- Empty `run_id` or non-existent `combiner_dir_local` →
  `SpecInvalid` (not silently False — the call shape was wrong).
- Sidecar with no `wave_map` → returns `all_waves_combined=True`
  trivially (nothing to verify). Caller must distinguish "wave
  experiment with no waves" from "non-wave experiment."
