---
name: resubmit-failed
verb: mutate
side_effects:
  - mutates: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (per-task retry counters; appends new_job_ids)
idempotent: true
idempotency_key: request_id (auto-derived from sorted failed_task_ids + category + sorted overrides when not supplied)
error_codes:
  - code: spec_invalid
    category: user
    retry_safe: false
    description: Empty failed_task_ids, missing category, or unknown category enum value.
  - code: journal_corrupt
    category: internal
    retry_safe: false
backed_by:
  cli: hpc-mapreduce resubmit --run-id <id> --spec spec.json [--experiment-dir <dir>]
  python: slash_commands.runner.resubmit_failed
exit_codes:
  - 0: ok
  - 1: spec_invalid
  - 3: journal_corrupt
---

## Purpose

Record a resubmission attempt in the journal: bump per-task retry counters, persist the failure category + applied resource overrides, and append the new scheduler job IDs to the active list. Like `submit-spec`, this primitive does NOT call qsub/sbatch — the caller is expected to have already issued the resubmission and captured the new job_ids.

## Compose with

- Common predecessors: `poll-run-status` (which surfaced the failures), `classify-failures` (which produced the `category`).
- Common successors: `poll-run-status` (track the new job_ids).

## Notes

- The `category` enum includes `gpu_oom`, `system_oom`, `walltime`, `node_fail`, `queue_stall`, `segv`. Unknown values fail validation.
- Idempotency: a replay with the same `request_id` returns `deduped: true` and does NOT increment per-task `attempts`. Useful for safe re-issue after a network drop between qsub and journal-write.
- Per-task `max_retries` lives in the profile config (default 3); enforcement is the slash command's responsibility, not this primitive's.
