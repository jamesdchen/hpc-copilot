---
name: monitor-flow
verb: workflow
side_effects:
- ssh: <cluster>
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (refreshes last_status)
idempotent: true
idempotency_key: run_id
error_codes:
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
  cli: hpc-agent monitor-flow --spec <path>
  python: hpc_agent.flows.monitor_flow.monitor_flow
---

## Purpose

**Workflow atom** that polls a `run_id` to terminal-or-budget. Internal loop: status report → combine newly-complete waves → tick log → terminal/budget check → sleep → repeat. Returns one envelope with `lifecycle_state` ∈ `{complete, failed, abandoned, timeout}` and a summary of actions taken.

Distinguished from [poll-run-status](poll-run-status.md) (the single-poll primitive): `monitor-flow` is the "wait until done" wrapper. Both write the same journal `last_status`; `monitor-flow` additionally writes the same `.monitor.jsonl` tick log that `/monitor-hpc` writes — so summary mode reads both surfaces' output uniformly.

Field-level contract: see `schemas/monitor_flow.input.json` and `schemas/monitor_flow.output.json`.

## Compose with

- Common predecessors: [submit-flow](submit-flow.md) or [submit-spec](submit-spec.md) — both produce the `run_id` this atom watches.
- Common successors: [aggregate-flow](aggregate-flow.md) when `lifecycle_state == "complete"`. On `"failed"`, the caller decides (skip iteration, escalate, retry).

## Polling cadence

The internal loop adapts its poll interval rather than using a fixed cadence:

- Fresh runs (< 10 min since submit): every 30s.
- Mid-life runs (< 1h): every 60s.
- Long-running (> 1h): every 5 min, escalating to 15 min after 4h.
- After any wave completes: re-poll within 30s so the combiner starts promptly.
- After 3 consecutive `unknown` task states: escalate, surfacing `escalation_reason: "n_unknown_runs_high"`.

Cadence is bounded by the spec's `wall_clock_budget_seconds`; pass `tick_interval_sec` to pin a fixed interval instead — rare, only when the caller has a specific cadence requirement.

## Notes

- **MVP does NOT auto-resubmit failed tasks.** When per-task failures appear with no work left, returns `lifecycle_state: "failed"` with `escalation_reason: "failed_tasks_no_auto_recover_in_mvp"`. The `/monitor-hpc` slash command handles auto-resubmit (with category-driven resource overrides); folding that into the atom requires a backend abstraction parallel to `submit-flow`'s and is tracked separately.
- **`timeout` is not failure.** When `wall_clock_budget_seconds` elapses without terminal state, the atom returns `timeout` and the cluster jobs continue running. Caller can re-invoke `monitor-flow` to keep watching, or schedule a follow-up tick.
- **Wave auto-combine** depends on the cluster-side reporter emitting a `waves` block in `last_status`. That happens iff the per-run sidecar carries a `wave_map`. No `wave_map` → `auto_combine_waves=true` is a silent no-op.
