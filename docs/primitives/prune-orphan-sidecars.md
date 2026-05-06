---
name: prune-orphan-sidecars
verb: mutate
side_effects:
- removes-files: <experiment>/.hpc/runs/*.json (orphans only)
idempotent: true
idempotency_key: experiment_dir
error_codes: []
backed_by:
  cli: hpc-mapreduce prune-orphan-sidecars
  python: claude_hpc.state.runs.prune_orphan_sidecars
---
# prune-orphan-sidecars

Delete every orphan sidecar under `<experiment>/.hpc/runs/`. An
*orphan* is a sidecar with no journal record (the run was never
recorded in `~/.claude/hpc/<repo_hash>/runs/<run_id>.json`),
typically left behind by a `submit-flow-batch` invocation that
crashed mid-loop after writing the per-spec sidecar but before the
journal record. Returns the list of pruned `run_ids` for caller
logging.

## Inputs

- `experiment_dir` (path) — experiment root.

No wire spec — Python-only signature; not exposed via `--spec`.

## Outputs

`list[str]` — run_ids whose sidecars were removed. Empty list when
no orphans exist (the common case).

## Side effects

- Removes files matching `<experiment>/.hpc/runs/<run_id>.json`
  for any sidecar without a corresponding journal record.

## Idempotency

Safe to invoke at any time. A second call after a successful one
returns `[]` (the orphans are already gone). Idempotency key:
`experiment_dir`.

## Notes

Auto-invoked by `submit-flow-batch` when it detects half-baked
sidecars at start-up; agents typically don't call it directly.
The slash-command `/submit-hpc` references this primitive when
`find-prior-run` reports `is_orphan=true` — surfaces the cleanup
hint to the user rather than silently mutating disk.

`prune-orphan-sidecars` does NOT touch the journal, only the
per-experiment sidecar tree. Journal entries with no surviving
sidecar are a separate consistency class handled by
`reconcile-journal`.
