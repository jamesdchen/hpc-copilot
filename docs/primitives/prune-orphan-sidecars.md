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

_Documentation pending._
