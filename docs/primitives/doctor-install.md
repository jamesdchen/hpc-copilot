---
name: doctor-install
verb: mutate
side_effects:
- scheduler: Windows Task Scheduler (schtasks) | POSIX crontab
- file_write: ~/.claude/hpc/<repo_hash>/doctor.spec.json + doctor.task.xml (Windows)
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent doctor-install --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.recover.doctor_install.doctor_install
---
# doctor-install

Puts the detection-only [`doctor`](doctor.md) watchdog onto the OS scheduler so a
missed driver-tick deadline — or an orphaned run left behind by a dead session —
is caught **out of session** (design §5, the dead-man's switch). The OS scheduler
is the bottom of the watch-the-watcher recursion: the one layer treated as boring
and reliable. It is **opt-in and never auto-installed**.

On install this verb writes a durable `doctor.spec.json` under the journal home
(`~/.claude/hpc/<repo_hash>/`) carrying `notify=true`, then registers a scheduler
task that runs `hpc-agent doctor --spec <that> --experiment-dir <dir>` every
`interval_minutes`. Because the scheduled scan reads the durable spec, its firing
is fully non-interactive. When it finds a stalled/orphaned run it raises an OS
notification carrying the drafted re-arm proposal — it does **not** print JSON
into a scheduler log nobody reads, and it **never** re-arms anything. A successor
session (or the human) answers `y`/nudge; safe recovery is already guaranteed by
tick idempotency.

Platform dispatch: Windows → Task Scheduler, registered from a **generated task
XML** (`schtasks /Create /F /TN <name> /XML <file>`); POSIX → a `crontab` marker
line. Installing only schedules the **detector**.

## Window hygiene (Windows)

The scheduled tick must never be visible, and two independent things make that
true:

- the generated XML carries `<Hidden>true</Hidden>`, and
- the action's `<Command>` is the **windowless** interpreter (`pythonw.exe`, the
  GUI-subsystem twin beside `python.exe` in every CPython install and venv
  `Scripts/` dir), which has no console to allocate in the first place.

Both, because `Hidden` is advisory — some hosts ignore it — and because
`schtasks /Create /TR "<command>"`, the shorthand this verb used before
2026-07-30, cannot express `Hidden` **at all**. That is why three
`hpc-agent-doctor-*` tasks flashed a console window in the operator's session
every 15 minutes, per task, for days. The XML also carries the cadence, so the
whole definition is one readable artifact under the journal home
(`doctor.task.xml`) rather than a `/TR` string plus flags.

## Lifecycle

A finished run must never leave a headless tick behind — the same rule
`decide-monitor-arm`'s `arm="none"` binds for the harness cron, applied to the
OS-scheduler arm. `hpc_agent.infra.local_scheduler.remove_watchdog_if_idle` is
the ONE definition and it is wired at the **guaranteed terminal harvest**
(`harvest_on_terminal`), which every terminal path passes through: the monitor
poll loop's terminal branches and its abnormal-exit `finally`, the reconcile
settle arm, and `settle-run`.

Because this task is namespaced per **repo**, not per run, the teardown
predicate is *"no live run remains in this experiment's journal namespace"* —
not *"this run finished"*. Removing it while a sibling run is still in flight
would blind the dead-man's switch for the run that still needs it.

Tasks that no terminal will ever fire for (installed by an older build, or
orphaned when a journal namespace was deleted) are caught by
[`doctor`](doctor.md)'s stale-watchdog probe, which alerts with the exact
removal command.

## Inputs

- `interval_minutes` (int, default `15`, ≥ 1) — how often the scheduler runs the
  scan. A cheap local filesystem read, so a tight cadence is fine.
- `uninstall` (bool, default `false`) — remove this experiment dir's scheduled
  doctor task instead of installing it.
- `notify` (bool, default `true`) — bake `notify=true` into the durable spec so
  the scheduled scan surfaces stalls as an OS notification.

## Outputs

`data` is a `DoctorInstallResult`:

```
{
  "status": "installed" | "already_installed" | "uninstalled" | "not_installed",
  "platform": "windows" | "posix",
  "task_name": "hpc-agent-doctor-<repo_hash>",
  "command": "\"<python>\" -m hpc_agent doctor --spec \"...\" --experiment-dir \"...\"",
  "interval_minutes": <int>,
  "spec_path": "~/.claude/hpc/<repo_hash>/doctor.spec.json",
  "notify": <bool>
}
```

## Errors

- `spec_invalid` — the underlying scheduler command (`schtasks` / `crontab`)
  reported a failure (e.g. insufficient privilege to create the task).

## Idempotency

Keyed by the derived `task_name` (`hpc-agent-doctor-<repo_hash>`), not a
`run_id`. Idempotent **by replacement**: re-installing rewrites the definition in
place (`/Create /F /XML` on Windows, a marker-keyed cron line rewrite on POSIX)
and returns `already_installed` — one task, never a duplicate, and a task
registered by an older build (visible window, stale cadence) **heals** instead of
surviving behind an existence check. `uninstall` on an absent task returns
`not_installed`. Existing unrelated crontab lines are preserved across
install/uninstall.

## Notes

The scheduled command uses `<python> -m hpc_agent` rather than the bare
`hpc-agent` console script so it survives the scheduler's minimal PATH. This verb
schedules detection only; the actual re-arm of a stalled driver is a separate,
human-gated action. If you need overnight coverage on the cluster side as well as
locally, this is the client-side half — the cluster-side watcher is installed
separately (design §5, the hybrid monitor).
