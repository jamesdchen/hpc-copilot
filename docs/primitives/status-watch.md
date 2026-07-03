---
name: status-watch
verb: workflow
side_effects:
- ssh: <cluster> (status polls)
- writes-tick-log: <experiment_dir>/<run_id>.monitor.jsonl
idempotent: true
idempotency_key: monitor.run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: journal_corrupt
  category: internal
  retry_safe: false
- code: precondition_failed
  category: user
  retry_safe: false
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-agent status-watch --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.status_blocks.status_watch
---
## Purpose

Status block **watch — blocking poll to terminal or anomaly**
(docs/design/human-amplification-blocks.md §3, §5). A thin orchestrator that
composes `monitor-flow` — which owns the throttled SSH spine (ConnectTimeout,
BatchMode, adaptive backoff) and the §5 guaranteed terminal harvest — then
digests the outcome into a **brief** and terminates at the first decision point.
No decision is resolved by the LLM.

The block chains deterministically through the poll loop (no human boundary
inside), then terminates:

- a **clean terminal** (`complete`) → `needs_decision=false` and a hand-off hint
  to the harvest block. The terminal harvest already ran inside `monitor-flow`'s
  `finally`, so the watch never re-harvests — it points at the marker.
- an **anomaly** (`failed` / `abandoned`) → `needs_decision=true` with a
  drafted-evidence brief: error digest, per-task counts, the failed-wave ledger,
  and a structured `recommendation` (proposed next-action DATA, never LLM text).
- a **timeout** (wall-clock budget hit, cluster jobs may run on) →
  `needs_decision=true`, the "keep watching or stop?" terminator; arms the next
  tick (`decide-monitor-arm`) when an `invocation_argv` is supplied.

## Inputs

A `StatusWatchSpec` JSON spec with:

- `monitor` — a nested [`MonitorFlowSpec`](monitor-flow.md) (run_id + poll cadence
  + wall-clock budget). status-watch runs it to terminal/timeout.
- `invocation_argv` (optional) — the exact `/monitor-hpc` argv the next tick
  should re-invoke. When supplied, a **timeout** arms the next tick and folds the
  cron/loop/none decision into the brief. Null skips arming.
- `user_invoked_via_loop` (optional) — true iff this tick runs under `/loop` (the
  user drives cadence; no cron armed).

## Outputs

A `StatusBlockResult` (`block="watch"`) with `stage_reached`, `needs_decision`,
and a `brief` carrying `{run_id, lifecycle_state, summary, combined_waves,
failed_waves, escalation_reason, ticks, elapsed_seconds}` plus, per terminator:

- `harvest_handoff` — on a clean terminal: `{guaranteed, harvest_marker,
  next_block}`.
- `anomaly` — on failed/abandoned: `{lifecycle_state, summary, failed_waves,
  escalation_reason, error_digest, recommendation}`.
- `monitor_arm` — on a timeout with `invocation_argv`: the `decide-monitor-arm`
  decision (`arm` / `cadence_sec` / cron args).

`stage_reached` ∈ `watch_terminal` · `watch_anomaly` · `watch_timeout`.

## Errors

- `spec_invalid` — malformed spec.
- `ssh_unreachable` — the status poll could not reach the cluster.
- `precondition_failed` — the run has no scheduler job ids (nothing to monitor).
- `remote_command_failed` — the cluster-side reporter failed.
- `journal_corrupt` — no journal record for the run.

## Idempotency

Idempotent on the run_id — re-invoking after a terminal return is a no-op (the
journal already carries the terminal state; each poll is itself idempotent, and
the terminal harvest is best-effort/append-only).

## Notes

Differs from `status-pipeline` (the deterministic wait+dispatch spine) by adding
the code-digested evidence brief, the harvest hand-off, and the timeout re-arm —
the block-grammar surface the human decides against. Pairs with `status-snapshot`
(the cheap journal-first digest that opens no connection loop).
