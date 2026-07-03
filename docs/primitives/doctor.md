---
name: doctor
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent doctor --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.recover.doctor.doctor
---
# doctor

Driver watchdog (dead-man's switch). Scans live (`in_flight`) runs for a missed
driver-tick deadline — a `next_tick_due` stamped by the driver that is now in the
past — and surfaces each stalled run as a DRAFTED recovery proposal plus the
detection evidence. Detection is its whole job: it **never** restarts or re-arms
anything (design §5, "The watchdog never restarts anything"). Safe recovery is
already guaranteed by tick idempotency, so the human only has to decide *whether*
to re-arm. Pure local filesystem read — no SSH, no scheduler — which makes it the
deterministic verb an OS-scheduled task (Task Scheduler / cron) runs
out-of-session; the watch-the-watcher recursion bottoms out at the OS scheduler.

## Inputs

- `now` (string, optional) — ISO-8601 UTC instant to evaluate deadlines against.
  Defaults to the current time; supply it for deterministic testing. A run is
  stalled when its `next_tick_due` is before this instant.
- `notify` (bool, default `false`) — when `true` and stalled runs are found,
  raise an OS notification carrying the drafted re-arm proposal (notify only,
  never acts). Default `false` keeps the in-session verb unchanged; the
  OS-scheduled installer ([`doctor-install`](doctor-install.md)) bakes
  `notify=true` into its durable spec so the out-of-session scan alerts instead
  of printing JSON nobody reads.

## Outputs

`data` is a `DoctorResult`:

```
{
  "now": "<iso-8601 utc>",
  "stalled_count": <int>,
  "stalled": [
    {
      "run_id": "<id>",
      "status": "in_flight",
      "last_tick_at": "<iso|null>",
      "next_tick_due": "<iso>",
      "cluster": "<name|null>",
      "ssh_target": "<user@host|null>",
      "proposal": "driver stalled since <ts>, status in_flight: ... re-arm?",
      "evidence": {"last_tick_at": ..., "next_tick_due": ..., "now": ..., "overdue_seconds": <int|null>}
    }
  ]
}
```

## Errors

- `spec_invalid` — `now` was supplied but is not a valid ISO-8601 UTC string.

## Idempotency

Pure read of derived state; no side effects. Re-running reflects whatever runs
are on disk at that instant. Not keyed on `run_id` — it scans the whole journal.

## Notes

A run with no `next_tick_due` stamped yet (never ticked) is **not** stalled —
absence of a deadline is not a missed one. The proposal string is drafted for a
`y`/nudge decision and is never enacted by this verb; re-arming a stalled driver
is a separate, human-gated action whose safety rests on tick idempotency.
