---
name: wait-detached
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent wait-detached --spec <path>
  python: hpc_agent.ops.monitor.wait_detached.wait_detached
---
## Purpose

**Blocking local wait** on a detached worker's exit — the harness-notification
bridge for detach-by-contract (design §3, §5). Handing a slow block to a
raw-`Popen` worker keeps the chat free but severs the harness's completion
channel: the driving agent has nothing to await and falls back to timed
`/loop` polling (guessed cadences, cache burn, up to a full poll interval of
dead air after the brief is ready). This verb restores an awaitable: it blocks
until the worker's lease pid (`_detached/<block>-<run_id>.lease.json`) dies or
the budget elapses. Launch it through the harness's native backgrounding
(Claude Code `run_in_background`) and the harness wakes you exactly once, when
the process exits — event-driven, no polling loop, no SSH (purely local pid
probes).

## Inputs

A `WaitDetachedInput` JSON spec with:

- `run_id` (str, strict run-id shape) — the run whose worker to await.
- `block` (str, optional) — the block whose worker to await (e.g.
  `submit-s2`). Omitted → wait on **any** live lease for the run; the first
  exit returns and names its block.
- `timeout_sec` (float, default `7200`, max `86400`) — wall-clock budget.
  Default generously: a detached canary/main watch can legitimately run for
  hours and the waiter is cheap.
- `poll_interval_sec` (float, default `2`, max `60`) — local pid-probe cadence.

## Outputs

A `WaitDetachedResult` — `{outcome, run_id, block, pid, log_path, waited_sec}`
with `outcome` ∈:

- `worker_exited` — the lease pid died: the block finished (or crashed); read
  the run's journal state next for the verdict.
- `no_live_worker` — no live lease at call time: the worker already exited
  (brief likely ready) or was never launched. Read the journal next either way.
- `timeout` — budget elapsed with the worker still alive. Not an anomaly by
  itself (long queue waits are normal); re-arm another wait or consult
  `status-snapshot`.

`block` is taken from the found lease (source of truth) when one exists, else
the input's passthrough; `log_path` points at the worker's log when the lease
carries one.

## Errors

- `spec_invalid` — malformed spec (run-id shape, out-of-range budget/interval);
  enforced at the wire boundary.

## Idempotency

Idempotent — a pure wait that writes nothing. Re-arming after a `timeout` or a
spurious wake is always safe; a wait on an already-exited worker returns
`no_live_worker` immediately.

## Notes

- Corrupt or mid-write lease files are skipped, never fatal — an unreadable
  lease must not strand the waiter; `no_live_worker` plus the journal remains
  the truthful answer.
- Deliberately **not** in the curated MCP catalog (its result carries no
  `next_block`): the MCP server dispatches tools synchronously in-process, and
  a multi-hour blocking call would wedge it. CLI-fallback-only by design; the
  skills route it through backgrounded Bash.
- The §5 watchdog is an untouched backstop: if this waiter dies with the
  session, only the notification is lost — doctor/the watchdog still catch the
  run.
