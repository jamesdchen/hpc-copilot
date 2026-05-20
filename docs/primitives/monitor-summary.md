---
name: monitor-summary
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent monitor-summary --experiment-dir <path> --run-id <id>
  python: hpc_agent.atoms.monitor_summary.monitor_summary
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Render the canonical user-facing tick summary by reading the run journal + the most recent line of `<experiment>/.hpc/runs/<run_id>.monitor.jsonl`. The slash command's Step 7 (Summary mode) prints the returned `headline` + `body` verbatim — no agent framing, no per-tick wording drift.

Returns `{lifecycle_state, headline, body, armed_hint}`. `armed_hint` is null when the run is terminal (the slash command exits) or a one-line note pointing the agent at `decide-monitor-arm` otherwise.

## Compose with

- **Predecessors**: `monitor-flow` (writes the journal record and the tick log this primitive reads).
- **Successors**: `decide-monitor-arm` (when not terminal — the slash command emits the summary, then the `armed:` line).

## Notes

- **Pure read-only**: no SSH, no journal writes, no cluster traffic. Safe in summary mode where the slash command is explicitly forbidden from contacting the cluster.
- **Robust to malformed JSONL**: lines that don't parse as a dict are skipped; the primitive returns the most recent valid record. A tick log truncated mid-write doesn't tank the summary.
- **No journal record**: returns `lifecycle_state="unknown"` with a clear `headline`. The slash command can show the message to the user without crashing.
- **Headline format**: `"run_id=X reached terminal state: Y"` for terminal, `"run_id=X in flight — counts"` otherwise. Byte-stable for the same input state.
