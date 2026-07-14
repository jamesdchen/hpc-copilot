---
name: logs
verb: query
outputs:
- name: run_id
  type: string
- name: scheduler
  type: string
  description: One of `slurm` / `sge`, resolved from clusters.yaml.
- name: logs
  type: array
  description: One element per fetched task — each carries `task_id`, `path`, and
    a `text` tail (≤ `lines` lines).
- name: note
  type: string
  description: Present only when `--all-failed` ran but the cluster reported zero
    failed tasks.
side_effects:
- ssh: <cluster>
idempotent: true
idempotency_key: none
error_codes:
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
- code: journal_corrupt
  category: internal
  retry_safe: false
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent logs [--experiment-dir <dir>] --run-id <run_id> [--task-id <task_ids>]
    [--all-failed] [--lines <lines>]
  python: hpc_agent.ops.monitor.logs_atom.fetch_logs
exit_codes:
- 0: ok
- 1: spec_invalid
- 2: ssh_unreachable / remote_command_failed
- 3: journal_corrupt / internal
---

## Purpose

Fetch per-task stderr tails from the cluster for a given `run_id`. The
two selection modes are mutually exclusive: an explicit `--task-id`
list (for hand-picking a few tasks to inspect) or `--all-failed` (the
common triage path — pull every failure for a fresh look).

## Compose with

- Common predecessors: `poll-run-status` or `monitor-flow` flagging
  failures; or an explicit `--task-id` list from a higher-level summary.
- Common successors: `failures` (cluster the fetched logs by stderr
  fingerprint) or `resubmit-failed` (after the operator has decided on a
  category).

## Notes

- The scheduler is resolved from `clusters.yaml`; if the cluster entry
  is missing or unparseable, it falls back to `slurm` so the fetch still
  proceeds best-effort.
- Per-task stderr paths follow each scheduler's convention; the helper
  `hpc_agent.infra.cluster_logs.fetch_task_logs` handles the formatting.
- Returns an empty `logs` list with a `note` rather than an error when
  `--all-failed` finds nothing — keeps the call composable from agent
  loops that re-check until a failure shows up.
