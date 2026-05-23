---
name: load-context
verb: query
inputs:
- name: experiment_dir
  type: path
  description: Repo root containing the journal and `.hpc/` state. Defaults to cwd.
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent load-context [--experiment-dir <dir>]
  python: hpc_agent.atoms.load_context.load_context
exit_codes:
- 0: ok
---

## Purpose

Reconstruct the workflow context for an experiment from on-disk state alone — run sidecars, the journal, and campaign cursors. A fresh-context step (a subagent, a restarted session, a cron tick) has no conversational memory of the active `run_id`, campaign, cluster, or config the previous step established; relying on that memory is unsafe because context compaction or a session restart erases it. `load-context` lets every skill open with one CLI call instead.

The envelope's `data` carries:

- `latest_run` — the newest run sidecar projected to its identity plus the v2 config snapshot (`cluster`, `profile`, `campaign_id`, `project`, `remote_path`, `resources`, `env`, `env_group`, `constraints`, `runtime`, `cmd_sha`, `result_dir_template`, `task_count`, `job_ids`, `is_orphan`), or `null` when no run exists.
- `in_flight` — journal records still in flight, one row each (`run_id`, `campaign_id`, `cluster`, `ssh_target`, `remote_path`, `job_ids`, `total_tasks`, `stage`, `status`, `last_status_age_seconds`).
- `campaigns` — every campaign with at least one sidecar, plus its cursor `iteration` and `last_run_id` when a cursor file exists.
- `next_step_hint` — `submit` / `monitor` / `aggregate`, derived purely from the in-flight set.
- `warnings` — non-fatal notes (orphan sidecar, unreadable cursor).

## Compose with

- **No predecessors.** Run this first, at the start of every multi-step skill (`hpc-submit`, `hpc-status`, `hpc-aggregate`, `hpc-campaign`) and at the start of every fresh-context subagent step.
- Common successors: `suggest-setup-action` (priority-ladder branch), `poll-run-status` / `monitor-flow` (per `run_id` from `in_flight`), `aggregate-flow` (per `latest_run`), `campaign-status` (per `campaign_id`).

## Notes

- Pure local read — no SSH, no scheduler. It composes `find_existing_runs` / `read_run_sidecar`, `find_in_flight_runs`, `campaign-list`, and `read_cursor`.
- `latest_run` surfaces exactly the config values a skill would otherwise cache in conversational memory. Read them from here; never from memory or shell variables.
- A cursor is only read when its campaign directory already exists, so the primitive creates nothing and keeps an empty `side_effects` contract.
- `is_orphan: true` on `latest_run` means the newest sidecar never landed a cluster job — resubmit it or run `prune-orphan-sidecars`.
