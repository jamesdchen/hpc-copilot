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
  cli: hpc-mapreduce verify-aggregation-complete --experiment-dir <path> --run-id
    <id> --combiner-dir <path>
  python: claude_hpc.atoms.aggregation_invariants.verify_aggregation_complete
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Walk the run sidecar's `wave_map` + the locally-pulled `_combiner/` dir; report the framework-knowable post-aggregate invariants. The user's aggregation OUTPUT is opaque (framework doesn't know what `qlike_score=0.42` means), but the INVARIANTS — every wave combined, every task accounted for, provenance present, no cross-run contamination — are framework-knowable.

Returns `{ok, run_id, all_waves_combined, missing_waves, all_tasks_present, missing_tasks, unexpected_tasks, provenance_present, expected_*, pulled_*}`. The agent reads `ok` and surfaces violations to the user.

## Compose with

- **Predecessors**: `aggregate-flow` (does the rsync_pull → reduce; this primitive checks the result).
- **Successors**: agent surfaces violations or proceeds with user-facing aggregation framing.

## Notes

- **Pure read-only.** Walks local files only; no SSH.
- **Skips `wave_*.runtime.json`** files — those are the warm-picker pipeline's per-wave runtime sidecars, not the aggregation partials.
- **`unexpected_tasks` catches cross-run contamination** — task_ids in the partials but not in the run's wave_map. A red flag the agent should escalate to the user instead of papering over.
