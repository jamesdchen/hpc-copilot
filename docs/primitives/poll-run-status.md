---
name: poll-run-status
verb: query
side_effects:
  - writes: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (refreshes last_status under flock)
  - writes: ~/.claude/hpc/<repo_hash>/runs/<run_id>.last_status.json (cached snapshot)
  - ssh: cluster reachable on submit cluster
idempotent: true
idempotency_key: run_id
error_codes:
  - code: journal_corrupt
    category: internal
    retry_safe: false
    description: No journal record for run_id; verify the id is correct.
  - code: ssh_unreachable
    category: network
    retry_safe: true
  - code: remote_command_failed
    category: cluster
    retry_safe: false
    description: The on-cluster reporter exited non-zero; surface stderr.
backed_by:
  cli: hpc-mapreduce status --run-id <id> [--experiment-dir <dir>]
  python: slash_commands.runner.record_status
exit_codes:
  - 0: ok
  - 2: ssh_unreachable / remote_command_failed (check retry_safe)
  - 3: journal_corrupt
---

## Purpose

SSH-issue a fresh status report for one run, refresh the journal's `last_status`, and write a cached snapshot. The single source of truth for "is this run done yet" — the `lifecycle_state` field drives every downstream decision.

## Compose with

- Common predecessors: `submit-spec` (the run_id this primitive polls), `list-in-flight` (to discover which run_ids exist).
- Common successors: `aggregate-results` (when `lifecycle_state == complete`), `resubmit-failed` (when failures appear), `combine-wave` (when a wave completed but isn't yet in `combined_waves`).
- Loop wrapper: the `/monitor-hpc` slash command calls this once per tick at an adaptive cadence (60s–3600s depending on ETA), writes a tick record, and self-schedules the next invocation.

## Notes

- One status call ≈ one SSH multiplexed channel; serialize calls per-cluster (most schedulers cap at ~1/sec).
- `lifecycle_state == abandoned` means the recorded `job_ids` are no longer known to the scheduler — usually the result of `reconcile-journal`. Do not silently interpret this as `complete`.
- `combined_waves` and `failed_waves` are mutated by `combine-wave`, not by this primitive — but they're surfaced here so a single poll tells the caller everything it needs to decide what to do next.
