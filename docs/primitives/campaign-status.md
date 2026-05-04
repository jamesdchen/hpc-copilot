---
name: campaign-status
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce campaign status --campaign-id <id> [--experiment-dir <dir>]
  python: hpc_mapreduce.reduce.history.prior
exit_codes:
- 0: ok
---

## Purpose

Report per-iteration reduced metrics for one closed-loop campaign. Walks every sidecar tagged with `campaign_id`, runs `reduce_metrics` on each iteration's result directories, returns the history dict-list. Pure local read; no SSH.

## Two interfaces, one operation

This primitive exposes the same operation through two surfaces, intentionally:

- **CLI envelope** (`hpc-mapreduce campaign status --campaign-id <id>`) — wraps the data in the standard `{"ok": ..., "data": {...}}` envelope. Use from agents, slash commands, external orchestrators, anything that consumes the framework over a shell boundary.
- **Python callable** (`from hpc_mapreduce.reduce.history import prior`) — returns the `history` list directly (just `list[dict]`, no envelope). Use from inside `.hpc/tasks.py` at module-load time, where shelling out to a subprocess from inside a Python module being loaded by another Python process would be wasteful (and recursive — `tasks.py` is itself loaded by the framework). The `prior()` Python entry point is the primary call site for closed-loop strategies (Optuna, walk-forward) that read history each iteration.

Both surfaces walk the same sidecars and produce the same per-iteration reduced metrics. The Python form's contract is the schema (`schemas/campaign.output.json#/$defs/status_data`)'s `history` field unwrapped from the envelope.

## Compose with

- Common predecessors: [submit-spec](submit-spec.md) or [submit-flow](submit-flow.md) with `campaign_id` set — the source of the sidecars this primitive walks.
- Common successors: another submit (next iteration). The campaign loop in `/campaign-hpc` reads this to decide whether to fire another iteration.

## Notes

- `history[i]` is `{}` when iteration `i`'s result directories don't exist on disk yet (still in flight or never produced output). Callers can filter with `[h for h in history if h]`.
- **Does NOT import `.hpc/tasks.py`.** This guarantee matters because closed-loop callers invoke `prior()` from inside their own `tasks.py` module body; an inner load would deadlock or recurse.
- Output validated against `schemas/campaign.output.json`'s `status_data`.
