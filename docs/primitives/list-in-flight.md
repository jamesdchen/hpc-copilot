---
name: list-in-flight
verb: query
inputs:
- name: experiment_dir
  type: path
  description: Repo root containing the journal. Defaults to cwd.
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent list-in-flight --experiment-dir <path>
  python: claude_hpc.atoms.list_in_flight.list_in_flight
exit_codes:
- 0: ok
- 3: journal_corrupt
---

## Purpose

List every journal record whose lifecycle is `in_flight`. The recovery path for a fresh Claude Code or agent session: discover what's still running before deciding whether to launch new work or resume monitoring.

## Compose with

- **No predecessors.** Run this first when a session starts cold and the user might have prior runs in flight.
- Common successors: `poll-run-status` (per `run_id`), `reconcile-journal` (when a record looks stale), `query-campaign` (to group records by `campaign_id`).

## Notes

- Pure local journal read; no SSH. The `last_status` field in each entry was set by the last `poll-run-status` tick — it can be arbitrarily stale.
- The slash-command surface (`/monitor-hpc`) groups by `campaign_id` when there are >3 in-flight records and at least one carries a campaign tag — that's surface logic, not part of this primitive.
- A run that the scheduler no longer knows about may still appear here as `in_flight` until `reconcile-journal` flips it to `abandoned`.
