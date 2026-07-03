---
name: kill
verb: mutate
side_effects:
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)
- ssh: <cluster>
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-agent kill --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.monitor.kill.kill
---
# kill

First-class run cancellation with journaled, verified semantics (design §5).
Given a `run_id`, `kill`: (1) journals the kill **intent** before any scheduler
mutation — durable even if the process dies mid-kill; (2) attempts scheduler
cancellation *through the backend seam* if a cancel affordance exists; (3)
verifies against the scheduler which of the requested job IDs are still alive;
(4) journals the subset confirmed gone; and (5) reports the honest
`N requested, N confirmed gone`. The count never claims more than the scheduler
confirms — if verification cannot run (SSH/transport failure), nothing is counted
as gone.

## Inputs

- `run_id` (string) — the run whose scheduler jobs should be killed. Its recorded
  `job_ids` are the kill target.
- `scheduler` (string) — backend/scheduler name, needed to query alive job IDs
  and (when the seam grows one) to build the cancel command. Validated against
  the live backend registry.

## Outputs

`data` is a `KillResult`:

```
{
  "run_id": "<id>",
  "requested_job_ids": [...],
  "confirmed_gone_job_ids": [...],
  "still_alive_job_ids": [...],
  "requested_count": <int>,
  "confirmed_count": <int>,
  "backend_cancel_attempted": <bool>,
  "backend_cancel_available": <bool>,
  "summary": "N requested, M confirmed gone",
  "requested_at": "<iso-8601 utc>",
  "confirmed_at": "<iso-8601 utc>"
}
```

## Errors

- `spec_invalid` — no journal record exists for `run_id`.
- `ssh_unreachable` / `remote_command_failed` — surfaced from the alive-check
  transport; on a verification failure the count reports 0 confirmed gone rather
  than overstating success.

## Idempotency

Keyed on `run_id`. Re-running re-journals the intent and re-verifies against the
scheduler; a job already gone stays in `confirmed_gone_job_ids`. Safe to replay.

## Notes

**Backend-cancel gap (integration item):** the backend seam does not today expose
a cancel-command builder (`build_cancel_cmd`), so `backend_cancel_available` is
`false` for every built-in backend and no `scancel`/`qdel` is dispatched. The
primitive still journals intent, verifies, and reports honestly — a run whose
jobs are still live will read `N requested, 0 confirmed gone`, surfacing the gap
rather than hiding it. When a backend grows `build_cancel_cmd(job_ids) -> str`,
the cancel path lights up automatically with no change to this primitive.
