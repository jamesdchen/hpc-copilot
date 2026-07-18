---
name: alerts-ack
verb: mutate
side_effects:
- file_write: ~/.claude/hpc/<repo_hash>/doctor.alerts.seen
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent alerts-ack [--spec <path>] [--experiment-dir <dir>]
  python: hpc_agent.ops.recover.alerts_ack.alerts_ack
---
# alerts-ack

Acknowledges the unacknowledged [`doctor`](doctor.md) watchdog alerts for an
experiment dir by advancing the `doctor.alerts.seen` watermark. Proving run #3
gave the alert log a "seen" watermark (detection without delivery is silence),
but the only way to advance it was as a side effect of a
`status-snapshot --mark-seen`. A human who saw the alerts on another surface —
the `doctor` envelope, the SessionStart count hook — had no direct way to dismiss
them. This verb closes that gap.

It advances the watermark to the newest alert currently in `doctor.alerts.log`
(or a caller-supplied `up_to_ts`), so those alerts stop counting as "new" on
every surface that reads `read_unacknowledged_alerts`. **Notify only, never act**
(§5): it never truncates the append-only alert log and never touches the cluster —
it only moves the watermark that decides which alerts are still unacknowledged.

## Inputs

- `up_to_ts` (str | null, default `null`) — acknowledge every alert at or before
  this ISO-8601 UTC instant. When omitted, acknowledges up to the newest alert in
  the log (or `now` if the log is empty/unreadable). No `--spec` at all is the
  common "dismiss what I've seen" case.

## Outputs

`data` is an `AlertsAckResult`:

```
{
  "acknowledged_up_to": "<iso-8601 utc>",
  "acknowledged_count": <int>,
  "remaining": <int>
}
```

- `acknowledged_up_to` — the instant the watermark was advanced to.
- `acknowledged_count` — how many previously-unacknowledged alerts this call
  cleared from the standing queue (before − after).
- `remaining` — unacknowledged alerts still newer than the watermark afterward
  (non-zero only if an alert carries a `ts` past `up_to_ts`).

## Errors

- `spec_invalid` — a supplied `--spec` file is not a JSON object or fails
  `AlertsAckSpec` validation.

## Idempotency

Keyed by `experiment_dir`. The underlying watermark write is **monotonic**: a
watermark already at or past the target is left untouched, so re-running never
resurrects an already-acknowledged alert and a stale call can never lower the
watermark. A second `alerts-ack` with nothing new to see reports
`acknowledged_count: 0`.

## Notes

The alert log (`~/.claude/hpc/<repo_hash>/doctor.alerts.log`) is an append-only
audit trail and is **never** modified by this verb — acknowledgment is purely the
sibling `doctor.alerts.seen` watermark. The canonical writer
(`notify._append_alert_log`) records one deduplicated JSON line per live stall, so
a single stall that re-ticks for hours acknowledges as one alert, not dozens.
Since a status snapshot with `--mark-seen` advances the same watermark, running a
snapshot and `alerts-ack` are interchangeable ways to dismiss surfaced alerts.
