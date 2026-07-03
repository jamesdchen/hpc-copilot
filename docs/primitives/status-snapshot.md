---
name: status-snapshot
verb: workflow
side_effects:
- ssh: <cluster> (only when reconcile=True)
idempotent: true
idempotency_key: run_id
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
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent status-snapshot --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.status_blocks.status_snapshot
---
## Purpose

Status block **snapshot — one-shot digest** (docs/design/human-amplification-blocks.md
§3, §5). A thin orchestrator that reads durable run state (journal-first, no
watch) and digests it into a **brief** for the `y`/nudge propose loop: *what is
running where* and *what changed since the human last looked*. No decision is
resolved by the LLM — the snapshot hands back the brief; the human greenlights or
nudges.

The block's load-bearing move (§5, first-class task state): the changed-since
delta is computed against each run's `last_seen_by_human_at` watermark, then the
watermark is re-stamped (via `mark_seen_by_human`) so the *next* snapshot's delta
is measured from this look. It sets `needs_decision` only on evidence — a stalled
driver (§5 dead-man's switch) or a run sitting on a failed/abandoned terminal —
never manufacturing a decision point.

## Inputs

A `StatusSnapshotSpec` JSON spec with:

- `run_id` (optional) — the run to digest. Null → a **fleet digest** over every
  in-flight run.
- `reconcile` (default `false`) — re-derive ground truth from the cluster
  (`reconcile-journal`) before digesting. This is the **only** path that touches
  SSH; it requires `run_id` and `scheduler`.
- `scheduler` (optional) — backend name; required only when `reconcile=true`.
- `now_iso` (optional) — ISO-8601 UTC "now" for the stalled-driver check and the
  watermark. Defaults to the current UTC time.
- `mark_seen` (default `true`) — re-stamp `last_seen_by_human_at` after computing
  the delta. Disable for a peek that must not move the watermark.

## Outputs

A `StatusBlockResult` (`block="snapshot"`) with `stage_reached`, `needs_decision`,
and a `brief`:

- `running_where` — one row per digested run: `{run_id, cluster, ssh_target,
  status, summary, last_tick_at, last_seen_by_human_at, changed_since_seen}`.
- `changed_since_seen` — the subset of `running_where` whose driver ticked since
  the human last looked (never looked → everything is new).
- `stalled_runs` — the `find_stalled_runs` hits (a live run whose `next_tick_due`
  is in the past). Detection only; the recommendation is a re-arm proposal — the
  watchdog never restarts anything (§5).
- `anomalies` — digested runs on a failed/abandoned terminal, each with a
  structured `recommendation` (proposed next-action DATA, not LLM prose).

`stage_reached` ∈ `snapshot_clean` (nothing demands a decision,
`needs_decision=false`) · `snapshot_anomaly` (a stalled driver and/or a
failed/abandoned run surfaced, `needs_decision=true`).

## Errors

- `spec_invalid` — `reconcile=true` without a `run_id` or a `scheduler`.
- `ssh_unreachable` — the reconcile re-derive could not reach the cluster.
- `cluster_unknown` — a cluster referenced in the reconcile is not in `clusters.yaml`.
- `journal_corrupt` — an on-disk run record is unreadable.

## Idempotency

Idempotent — a read-and-digest pass. The only durable write is the
`last_seen_by_human_at` watermark (monotonic advance to `now_iso`); re-running is
safe. The optional reconcile is itself idempotent.

## Notes

Journal-first and cluster-free unless `reconcile=true`. Pairs with `status-watch`:
the snapshot answers "where do things stand right now" without opening the
connection loop; the watch blocks on the throttled SSH spine to a terminal.
