---
name: read-runtime-prior
verb: query
inputs:
- name: profile
  type: string
- name: cluster
  type: string
- name: cmd_sha
  type: string
  description: Filter samples to one cmd_sha (recommended after .hpc/tasks.py edits).
  default: null
- name: experiment_dir
  type: path
  default: cwd
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-mapreduce runtime-prior --profile <name> --cluster <name> [--cmd-sha <sha>]
  python: claude_hpc.orchestrator.runtime_prior.summarize
exit_codes:
- 0: ok
- 1: spec_invalid
- 3: internal
---

## Purpose

Roll up `<repo>/.hpc/runtimes/<profile>.<cluster>.json` samples into per-`gpu_type` quantiles. Drives `score-submit-plan`'s walltime selection. Read-only, local — no SSH.

## Compose with

- Common predecessors: a finished run that wrote samples (via `runtime_prior.append_sample`).
- Common successors: `score-submit-plan` (which calls this primitive internally).

## Notes

- `needs_canary: true` means there are no priors for this (profile, cluster) pair; the caller should submit a 1-task canary, ingest its elapsed time via `runtime_prior.append_sample`, then re-call.
- Filter by `cmd_sha` after editing `.hpc/tasks.py` since the new task list may have wildly different runtime characteristics; reusing the old samples would be misleading.
