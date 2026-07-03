---
name: aggregate-run
verb: workflow
side_effects:
- ssh: <cluster> (wave combine + rsync pull)
- sync-pull: <ssh_target>:<remote_path> -> <experiment_dir>/_aggregated/
idempotent: true
idempotency_key: aggregate.run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
- code: precondition_failed
  category: user
  retry_safe: false
- code: journal_corrupt
  category: internal
  retry_safe: false
backed_by:
  cli: hpc-agent aggregate-run --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.aggregate_blocks.aggregate_run
---
# aggregate-run

Combine + reduce + extract block for the aggregate flow (human-amplification
blocks, `docs/design/human-amplification-blocks.md` §3 — the finer grain of
submit's S4). A thin orchestrator that runs the deterministic `aggregate-flow`
pipeline (ensure waves combined → pull partials → reduce) and digests the
reduced metrics into a code-extracted results table, then TERMINATES at a human
decision point.

The load-bearing invariant: **results are never interpreted raw by the LLM**
(§2, the #355 doctrine extended from computing to concluding). Code extracts the
results table and the error sweep; the brief hands over an EMPTY
`proposed_interpretations` slot the LLM fills at the `y`/nudge boundary. The
human concludes from the numbers; the code never does.

## Inputs

- `aggregate` (`AggregateFlowSpec`) — the aggregate-flow spec: ensures waves
  combined, pulls partials, reduces. Its terminal-status precondition gate is
  `aggregate-run`'s own invariant — `ensure_all_combined=false` is the
  deliberate-partial opt-in that bypasses it.

## Outputs

`AggregateBlockResult` — `{block: "run", stage_reached, needs_decision, reason,
run_id, brief}`.

`stage_reached` is one of:

- `harvested` — every wave combined cleanly; the results table is complete.
- `harvest_partial` — some waves escalated (a combiner failure or an
  incomplete-wave sweep); the results table is over a subset.

Both terminators set `needs_decision: true` — the human reviews the table and
chooses an interpretation.

`brief` carries the code-extracted `results_table` (row-per-key, sorted), the
`error_sweep` summary (`escalation_reason`, `nonempty_failing_task_ids`,
`column_violations`), the `harvest_ledger` tail (wave-1's `harvest_on_terminal`
corroboration, read-only), and the EMPTY `proposed_interpretations` slot.

## Errors

- `spec_invalid` — malformed spec or aggregate-flow spec validation.
- `precondition_failed` — the run is not terminal and `ensure_all_combined` is
  set (the composed aggregate-flow gate; aggregate-run's own invariant).
- `ssh_unreachable`, `remote_command_failed` — cluster/rsync layer errors.
- `journal_corrupt` — no journal record for the run.

## Idempotency

Every step of the composed `aggregate-flow` is idempotent (combine-wave dedups,
rsync is a directory sync, reduce is a pure function). Re-running on the same
`run_id` is safe and cheap and produces byte-identical aggregated metrics.

## Notes

`aggregate-run` OWNS the terminal-or-explicitly-partial invariant via the
composed `aggregate-flow` gate — it does NOT assume `aggregate-check` ran first.
The `harvest_ledger` reference is read-only: aggregate-run never writes the
`<run_id>.harvest.jsonl` ledger, it only surfaces the sweeper's last marker as
corroborating evidence in the brief.
