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
- code: spec_invalid
  category: user
  retry_safe: false
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-agent aggregate-flow [--spec <path>] [--experiment-dir <dir>] [--dry-run]
    [--run-id <run_id>]
  python: hpc_agent.ops.aggregate_flow.aggregate_flow
---

## Purpose

**Workflow atom** that finalizes a run's aggregated metrics. Pipeline: ensure every wave in the sidecar's `wave_map` is combined on the cluster (running [combine-wave](combine-wave.md) for any missing) → rsync `_combiner/` partials locally → `reduce_partials` over them → optionally rsync per-task summary files. Returns one envelope with paths + the merged metrics dict.

Distinguished from [combine-wave](combine-wave.md) (the per-wave primitive): `aggregate-flow` is the "produce the final number" wrapper, calling `combine-wave` once per missing wave + handling the cross-wave merge.

Field-level contract: see `schemas/aggregate_flow.input.json` and `schemas/aggregate_flow.output.json`.

## Compose with

- Common predecessors: [submit-flow](submit-flow.md) (or [submit-spec](submit-spec.md)) + [monitor-flow](monitor-flow.md) — by the time `aggregate-flow` runs, the run has typically finished and most waves are already combined by `monitor-flow`'s in-flight pipelining.
- Common successors: optional profile-specific `aggregate_cmd` (the slash command's Step 4 — arbitrary user-defined cluster-side command); human-facing interpretation in `/aggregate-hpc`.

## Scope gate + look ledger

`aggregate-flow` is the ONE reduction seam, so it carries the rigor-primitives
scope machinery. A *scope* is an opaque caller-owned tag on the run sidecar's
`scopes` list that the framework never interprets; a *lock* on a scope is
deliberate human state (an embargo on a held-out scope, a reserved look).

- **Scope gate.** Before ANY SSH / combine / reduce work — a pure LOCAL read of
  the sidecar and each scope's decision journal, no SSH — the flow asserts that
  none of the run's scopes is currently locked. If one is, it raises
  `ScopeLocked` (`precondition_failed` error_code) naming the tag and its lock
  timestamp, and refuses to reduce: reducing a locked scope would spend a look
  the human meant to reserve. Both the interactive aggregate and the automatic
  terminal harvest refuse rather than spend it. There is exactly **one exit** —
  a human-journaled scope-unlock via `append-decision`
  (`scope_kind='scope'`, `block='scope-unlock'`, `resolved={'scope_action':
  'unlock'}`) naming the tag. There is no code override. A missing sidecar or a
  scope-less run passes silently — the gate can never false-trip.
- **Look-ledger side effect.** On a success terminal, for every scope tag the
  flow FIRST snapshots the scope's prior look counts (PRIOR by construction —
  this run's look is not on the ledger yet) THEN records one look, deduped on
  `(scope, run_id)` so a replay of the same run re-reports the same counts and
  never double-counts. The snapshot is returned as `scope_looks` (see below).

## Outputs

- `scope_looks` — a per-scope-tag map `{tag: {prior_looks, distinct_lineages}}`,
  or `null` (key omitted in spirit) for a scope-less run so existing consumers
  are untouched. `prior_looks` is the number of runs whose results were reduced
  against the scope BEFORE this reduction's own look was recorded;
  `distinct_lineages` collapses supersession-chained reruns of the same
  experiment to one. Two plain integers per tag — the framework counts looks, it
  never interprets what they found. `aggregate-run` copies this field verbatim
  onto its brief.

## Notes

- **Idempotent in two senses**: re-invoking on a fully-combined run is a no-op (`combine-wave` skips already-combined waves), and re-invoking after a partial pull just re-runs the pull (rsync handles the diff).
- **Per-trial QLIKE-style aggregation** for stochastic strategies (Optuna trials etc.): `aggregated_metrics` is a dict keyed by run_id (or grid-point key). Strategy callers iterate it to feed back into their study.
- **Profile-specific aggregate commands** are NOT inside this atom. When the per-run sidecar's `aggregate_defaults.aggregate_cmd` is set, the slash command runs it after `aggregate-flow` returns — the framework doesn't introspect those commands.
