---
name: watcher-install
verb: mutate
side_effects:
- ssh: <cluster>
- scheduler-submit: <cluster> (job rung only)
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
  cli: hpc-agent watcher-install [--experiment-dir <dir>] --run-id <run_id> [--action
    <action>] --scheduler <scheduler> [--stale-sec <stale_sec>] [--interval-min <interval_min>]
  python: hpc_agent.ops.monitor.watcher_install.watcher_install
---
# watcher-install

Install (or uninstall, or report the status of) a **cluster-side heartbeat
watcher** that survives the laptop (design §5, hybrid monitor). The client-side
supervisor reads a run's status cheaply over the throttled SSH spine; this verb
puts the other half — a short-lived watcher that fires cluster-side — in place
so an overnight run is not blind when the laptop sleeps.

The watcher form is chosen by an **install-time probe ladder**, never encoded
site policy. Every probe runs over the throttled spine
(`hpc_agent.infra.remote.ssh_run`); the scheduler is dispatched through the
backend seam (never a concrete-backend import):

1. **user `crontab`** — selected when `crontab -l` is viable (the command
   exists and the user is not blocked by `cron.deny`; an empty table is fine).
2. **`scrontab` (Slurm only)** — when the run's scheduler is the Slurm family
   (decided through the seam) and `scrontab -l` is viable.
3. **self-resubmitting job** — a minimal watcher job shipped as
   `.hpc/watcher/hpc_watcher_job.sh` and submitted through the backend seam
   (`build_remote_backend(...).submit_one(array=False)`); it runs the watcher,
   sleeps one interval, and resubmits itself with the scheduler's submit binary
   (read from the scheduler profile). Reached only when the seam exposes a
   submit binary for the scheduler.
4. **none available** — install NOTHING and say so **loudly**: `installed:
   false`, `mechanism: "none"`, and a reason stating that overnight blindness
   persists.

Install ships the stdlib-only watcher script
(`hpc_agent/execution/mapreduce/templates/watcher/hpc_watcher.py`, which never
imports `hpc_agent`) to `<remote_path>/.hpc/watcher/` and registers the
cron/scrontab line **idempotently** — keyed on a `# hpc-agent-watcher
run_id=<id>` marker comment, so a re-install strips the prior line before
appending a fresh one. `uninstall` strips the markers and removes the shipped
files; `status` reports which rung (if any) is installed.

## The watcher script contract

Each firing (cron/scrontab/job re-fires it; it never loops), for every run
directory it is pointed at (the project root `<remote_path>`):

- (re)writes `<run>/.hpc_watcher_status.json` — a heartbeat `{ts, run_dir,
  job_name, stale_sec, last_read, last_read_age_sec, alarm}`;
- reads `<run>/.hpc_last_read` (stamped by the laptop client on every
  `poll-run-status`) and, when it is missing or older than `--stale-sec`,
  writes `<run>/.hpc_watcher_ALARM` naming the staleness. When the client is
  reading again, a stale ALARM is cleared so a transient laptop-sleep blip
  self-heals.

The client half lives in `poll-run-status` (`record_status`): the same status
ssh call stamps `.hpc_last_read` and reads back `.hpc_watcher_ALARM`, surfacing
it in `last_status` under `watcher_alarm`. Either side dying is loud — a dead
client makes the watcher alarm; a dead watcher makes the heartbeat `ts` go
stale.

## Inputs

- `run_id` (string) — the run to watch. Its journal record supplies
  `ssh_target` + `remote_path`.
- `scheduler` (string) — backend/scheduler name; gates the scrontab rung and
  supplies the job rung's submit binary, both through the backend seam.
- `action` (`install` | `uninstall` | `status`, default `install`).
- `stale_sec` (int, default 1800) — alarm threshold for `.hpc_last_read`.
- `interval_min` (int, default 10) — watcher firing cadence.

## Outputs

`data` is a `WatcherInstallResult`:

```
{
  "run_id": "<id>",
  "action": "install|uninstall|status",
  "installed": <bool>,
  "mechanism": "cron|scrontab|job|none",
  "reason": "<human-readable outcome; the loud message on mechanism=none>",
  "detail": "<cron line / job id / probe failures>",
  "probes": {"crontab": "...", "scrontab": "...", "job": "..."}
}
```

## Errors

- `spec_invalid` — no journal record exists for `run_id`.
- `ssh_unreachable` / `remote_command_failed` — surfaced from the probe /
  ship / register ssh calls.

## Idempotency

Keyed on `run_id`. Re-installing strips the prior marker line before appending,
so cron/scrontab never accumulate duplicates; re-shipping the script overwrites
in place. Safe to replay.
