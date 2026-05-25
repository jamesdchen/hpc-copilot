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
  python: hpc_agent.ops.monitor.reconcile.mark_terminal
exit_codes:
- n/a (Python-only)
---
# mark-run-terminal

> **Internal primitive.** Python-only — no `cli=` invocation.
> Composed by `monitor-flow` and used by slash commands as the
> escape hatch for explicit lifecycle transitions.

Flip a run's `lifecycle_state` to a terminal value (`complete`,
`failed`, or `abandoned`). Thin pass-through to
`hpc_agent.state.journal.mark_run` for symmetry with the rest of
the runner layer.

## Composers

- `monitor-flow` (registered: `composes=[record_status,
  mark_terminal]` — the workflow flips a run terminal once the
  poll loop exits with a definitive lifecycle).
- `/monitor-hpc` slash command, after the user confirms
  abandonment of a stuck run.
- Any agent path that exhausts retries on a non-recoverable
  failure.

## Invariants

- **The wrapper is the only thing that takes the file lock.**
  Slash commands MUST go through this primitive rather than
  writing `lifecycle_state` directly to the journal — direct
  writes race with `poll-run-status` and `reconcile-journal`.
- **Terminal is terminal.** A subsequent `poll-run-status` call
  on the same `run_id` will refuse to flip the state back to
  `in_flight` (`hpc_agent.state.journal.mark_run` enforces).
- **Idempotent on `(run_id, status)`.** Calling twice with the
  same status is a no-op; calling twice with conflicting
  statuses raises (lifecycle integrity).

## Coupling

- The terminal-status enum (`complete`, `failed`, `abandoned`)
  must stay aligned with `_shared.py:LifecycleStateTerminal`
  (which the workflow output schemas use). Adding a terminal
  state means: extending the Literal, threading through
  `hpc_agent.state.journal.mark_run`, updating the wire schemas
  (auto), and reviewing every consumer that branches on the enum.

## Failure modes

- Caller passes `status="in_flight"` → `ValueError` (the natural
  lifecycle handles that; this primitive is for terminal flips).
- Concurrent `mark-run-terminal` + `reconcile-journal` on the
  same run_id → both take the flock; the second observes the
  first's write and either no-ops (same status) or raises
  (conflicting status).
