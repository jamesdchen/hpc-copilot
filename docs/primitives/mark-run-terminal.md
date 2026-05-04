---
name: mark-run-terminal
verb: mutate
inputs:
- name: experiment_dir
  type: path
- name: run_id
  type: string
- name: status
  type: enum
  description: One of `complete`, `failed`, `abandoned`. Cannot be `in_flight` (use
    the natural lifecycle for that).
outputs:
- name: run_id
  type: string
- name: lifecycle_state
  type: enum
side_effects:
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)
idempotent: true
idempotency_key: run_id
error_codes:
- code: journal_corrupt
  category: internal
  retry_safe: false
backed_by:
  cli: (none — Python-only primitive)
  python: slash_commands.runner.mark_terminal
exit_codes:
- n/a (Python-only)
---

## Purpose

Flip a run's `lifecycle_state` to a terminal value (`complete`, `failed`, or `abandoned`). The atomic-ops layer's escape hatch for cases where lifecycle has to be set explicitly rather than derived from `poll-run-status` / `reconcile-journal`.

## Compose with

- Common predecessors:
  - `poll-run-status` returning `summary.complete == total_tasks` and `post-flight: OK` → mark `complete`.
  - User abandons a stuck run → mark `abandoned`.
  - All retries exhausted on a non-recoverable failure → mark `failed`.
- Common successors: none — this is a terminal action.

## Notes

- Slash commands MUST go through this primitive rather than writing `lifecycle_state` directly to the journal — the wrapper is the only thing that takes the file lock.
- A subsequent `poll-run-status` call on the same `run_id` will refuse to flip the state back to `in_flight` — terminal is terminal.
