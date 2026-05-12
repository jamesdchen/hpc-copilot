---
name: failures
verb: query
inputs:
- name: run_id
  type: string
- name: experiment_dir
  type: path
  default: cwd
- name: lines
  type: integer
  default: 30
  description: Per-task stderr tail length used for fingerprinting.
outputs:
- name: run_id
  type: string
- name: failed_count
  type: integer
- name: scheduler
  type: string
- name: clusters
  type: array
  description: One element per fingerprint cluster. Each carries `category` (one of
    the canonical failure categories), `task_ids`, a representative `fingerprint`,
    and (when an `auto_retry` policy is configured for the run) a list of `retry_eligible_task_ids`.
- name: auto_retry_policy
  type: object
  description: Echoed only when an auto-retry policy is configured for this run.
- name: note
  type: string
  description: Present only when the fresh status poll reports zero failed tasks.
side_effects:
- ssh: <cluster>
idempotent: true
idempotency_key: none
error_codes:
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: journal_corrupt
  category: internal
  retry_safe: false
backed_by:
  cli: hpc-agent failures --run-id <id> [--lines <n>]
  python: claude_hpc.atoms.failures.fetch_failures
exit_codes:
- 0: ok
- 2: ssh_unreachable / remote_command_failed
- 3: journal_corrupt / internal
---

## Purpose

Re-poll status, fetch stderr for every failed task, and group them by
fingerprint so 40 failures with the same root cause show up as one
cluster instead of 40 logs to read. Each cluster carries an inferred
`category` from the canonical failure-category vocabulary (see
`claude_hpc/agent_cli.py::_VALID_RESUBMIT_CATEGORIES`).

When an auto-retry policy is configured for the run, each cluster is
annotated with which task ids are still eligible for an automated
retry per the per-category `max_attempts`. This is purely advisory —
the actual resubmit remains the caller's job (matches `/resubmit`).

## Compose with

- Common predecessors: any check that reports failed tasks
  (`poll-run-status`, `monitor-flow`).
- Common successors: `resubmit-failed` (per-category retry is the next
  obvious step) or `logs` (drill into one cluster's traceback).

## Notes

- Returns an empty `clusters` list with a `note` rather than an error
  when the fresh status poll reports no failed tasks.
- The category vocabulary is the union of the auto-classifier's
  emitted categories and the human-supplied taxonomy; the per-test
  invariant in `tests/test_resubmit_batching.py` keeps the two surfaces in sync.
- Idempotent in the sense that a re-poll over the same set of failed
  tasks produces the same clustering — the fingerprinting is
  deterministic.
