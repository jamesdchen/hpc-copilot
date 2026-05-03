---
name: check-preflight
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
  - code: internal
    category: internal
    retry_safe: false
    description: Surface to the caller; preflight bug.
backed_by:
  cli: hpc-mapreduce preflight [--cluster <name>]
  python: hpc_mapreduce.preflight.run
exit_codes:
  - 0: all checks passed
  - 2: one or more checks failed (envelope is still ok=true; failures live in checks[].ok)
---

## Purpose

Verify the local environment can submit HPC jobs: SSH agent reachable, `ssh` and `rsync` on PATH, `clusters.yaml` parses cleanly, optionally one cluster's TCP :22 reachable. Pure read; never mutates anything.

## Compose with

- **No predecessors.** Run this first in any pipeline that touches SSH (every submit-spec / poll-run-status / aggregate-results pipeline).
- Common successors: `discover-executors`, `score-submit-plan`, `submit-spec`.

## Notes

- Failures land as `checks[].ok = false` rather than an error envelope, so callers must inspect `data.all_ok` (or the exit code: 2 means at least one check failed).
- Skill callers can short-circuit: if a previous tick / session already ran `check-preflight` successfully, no need to repeat unless the env changed (new shell, restored backup, etc.).
- The TCP-22 probe is the only network-touching check; omit `--cluster` for an offline-only sanity pass.
