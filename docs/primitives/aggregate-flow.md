---
name: aggregate-flow
verb: workflow
side_effects:
- ssh: <cluster>
- sync-pull: <ssh_target>:<remote_path> -> <experiment_dir>/_aggregated/
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json
idempotent: true
idempotency_key: run_id
error_codes:
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: combiner_failed
  category: cluster
  retry_safe: true
- code: outputs_missing
  category: cluster
  retry_safe: true
- code: journal_corrupt
  category: internal
  retry_safe: false
- code: precondition_failed
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent aggregate-flow --spec <path> [--experiment-dir <dir>] [--dry-run]
  python: hpc_agent.ops.aggregate.flow.aggregate_flow
---

## Purpose

**Workflow atom** that finalizes a run's aggregated metrics. Pipeline: ensure every wave in the sidecar's `wave_map` is combined on the cluster (running [combine-wave](combine-wave.md) for any missing) → rsync `_combiner/` partials locally → `reduce_partials` over them → optionally rsync per-task summary files. Returns one envelope with paths + the merged metrics dict.

Distinguished from [combine-wave](combine-wave.md) (the per-wave primitive): `aggregate-flow` is the "produce the final number" wrapper, calling `combine-wave` once per missing wave + handling the cross-wave merge.

Field-level contract: see `schemas/aggregate_flow.input.json` and `schemas/aggregate_flow.output.json`.

## Compose with

- Common predecessors: [submit-flow](submit-flow.md) (or [submit-spec](submit-spec.md)) + [monitor-flow](monitor-flow.md) — by the time `aggregate-flow` runs, the run has typically finished and most waves are already combined by `monitor-flow`'s in-flight pipelining.
- Common successors: optional profile-specific `aggregate_cmd` (the slash command's Step 4 — arbitrary user-defined cluster-side command); human-facing interpretation in `/aggregate-hpc`.

## Notes

- **Idempotent in two senses**: re-invoking on a fully-combined run is a no-op (`combine-wave` skips already-combined waves), and re-invoking after a partial pull just re-runs the pull (rsync handles the diff).
- **Per-trial QLIKE-style aggregation** for stochastic strategies (Optuna trials etc.): `aggregated_metrics` is a dict keyed by run_id (or grid-point key). Strategy callers iterate it to feed back into their study.
- **Profile-specific aggregate commands** are NOT inside this atom. When the per-run sidecar's `aggregate_defaults.aggregate_cmd` is set, the slash command runs it after `aggregate-flow` returns — the framework doesn't introspect those commands.
