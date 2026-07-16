---
name: aggregate-stream
verb: query
side_effects:
- ssh: <parents> (per-arm announce census)
- sync-pull: <remote_path>/results/**/<summary> → local mirror
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: precondition_failed
  category: user
  retry_safe: false
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-agent aggregate-stream --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.aggregate.stream.stream_aggregate
---
# aggregate-stream

Emit a **partial-but-honest** aggregate over the arms that are complete **right
now**, instead of the all-or-nothing final harvest. Given one run or a set of
parent run_ids, `aggregate-stream`:

1. **censuses per-arm completeness** — reads the cluster's per-task terminal
   announcements (`read_announced_task_ids`) and joins them to the sidecar
   `wave_map` so each arm is `COMPLETE` iff **every** task in it announced
   `.complete`, `PENDING` otherwise;
2. **reduces ONLY the complete arms** through the run's own deterministic reducer
   (`reduce_metrics` per complete arm, or `multi_parent_reduce` when a persisted
   ownership map names a source+derived pair) — every emitted number is
   reducer-computed, never the LLM;
3. **emits a partial `metrics_aggregate.json`** carrying `arms_complete` plus an
   `arms_pending: [{arm, tasks_done, tasks_expected, owner_run_id}]` disclosure
   block — every incomplete arm is named, never silently capped;
4. **refines monotonically** — each call bumps `snapshot_seq`, reports
   `newly_complete` (the delta since the prior snapshot), and discloses any
   `arms_regressed` (an arm complete before but not now).

It **actuates nothing** — no submit, no kill, no journal terminal, no greenlight —
and is safe to re-call until every arm lands. This mechanizes the progressive
"40-arm now, 44 when the last leg lands, with `xgb/vol_demand` disclosed PENDING"
table by hand, deterministically.

## Scope (v1)

Streams **wave-aligned** runs only (an arm = a whole wave, provable from the
sidecar `wave_map`). A run whose `wave_map` is absent or does not cleanly
partition its task range **refuses** ("arm grouping not declared; final harvest
only") rather than guess a grouping and risk emitting a half-drained arm's wrong
`n`. A parent with no per-task census (pre-announce run / dispatcher not started)
refuses; **zero** complete arms refuses with the pending arms named. See
`docs/plans/streaming-aggregate-2026-07-16.md` §8 for the task→arm join invariant.

## Input

Exactly one of:

- `run_id` — stream a single run's arms.
- `parents: [run_id, …]` — stream a multi-leg run (each parent owns its arm
  space; a persisted ownership map dedupes a raced cell present under two legs).

Optional `output_dir` overrides the snapshot directory (default
`<experiment>/_aggregated/<key>/`); a stable key lets each call refine the same
snapshot.

## Output

`StreamAggregateResult` — `arms_complete`, `arms_pending` (by name), the
`aggregated_metrics` weighted-mean over all complete arms, `per_arm_metrics` (the
progressive table rows keyed `owner:arm`), the monotonic `snapshot_seq` /
`superseded` / `newly_complete` / `arms_regressed`, and the never-masked census
`disagreement`. Actuates nothing; overwrites only the snapshot file.
